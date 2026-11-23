"""Конвейер 3.2, раунд 1: досрочное снятие аренды, предел ожидания, тип кризиса у сообщения (A1).

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-23 21:00:00

ТЗ §2 п.2, §4.5 п.2, §9.3, §10; DECISIONS.md АР-44, АР-45; ревью 3.2 MAJOR-2, MAJOR-3, MINOR-1,
MINOR-8; тесты 3.2 D1, О-1…О-3; BACKLOG Б-83.

Отдельная миграция, а не правка 0013 на месте: 0013 уже в ветке, на ней построен воркер A3 и
прогнаны ревью и тесты в чужих тестовых БД; новая ревизия не требует пересоздавать их базы
(АР-4: правка на месте допустима, но здесь ничего не выигрывает).

1. **Снятие аренды и отложенный повтор** (АР-45 п.2, MAJOR-2, MINOR-1).
   ``pipeline_release(p_message uuid, p_token uuid, p_retry_after interval, p_count_attempt
   boolean) RETURNS boolean`` — по своему токену и только в ``processing``: токен снимается,
   ``work_lease_until`` := ``now() + p_retry_after`` (захват не выдаёт сообщение до этого
   времени; NULL или 0 — сразу), при ``p_count_attempt = false`` попытка не расходуется
   (счётчик −1, не ниже 0). ``false`` — аренда не своя / сообщение уже не в конвейере. 22023 —
   ``p_retry_after`` вне [0, 1 день] или ``p_count_attempt`` NULL. Меняет одну строку сообщения
   (других блокировок нет — как ``app_mark_read``). CHECK аренды: «токен ⇒ срок» вместо
   «токен ⇔ срок» (без токена срок — «не раньше»).
   ``pipeline_claim`` (та же сигнатура): свободно, если ``work_lease_until`` NULL или прошёл;
   **предел ожидания 24 ч от ``sent_at``** — старше → ``withheld/attempts_exhausted`` без модели
   (после ``couple_ended``: распавшаяся пара важнее).

2. **Тип кризиса у сообщения** (АР-45 п.3, MAJOR-3). Колонка ``withheld_crisis
   safety_event_type`` — главный тип для протокола автору (шаблон отправителя при
   ``threat_outgoing``, §9.3); CHECK: только при ``withheld_reason = 'safety'`` и не
   ``underage``. ``pipeline_withhold_leased(p_message, p_token, p_reason, p_events, p_crisis
   safety_event_type) RETURNS boolean`` — новая перегрузка: ``safety`` требует ``p_crisis``
   (не ``underage``), прочие причины — ``p_crisis`` NULL, иначе 22023. Прежняя 4-аргументная
   (0013) оставлена как обёртка: для ``safety`` тип — первый элемент ``p_events`` (главный, как
   ``Delivery.safety_events``); снимается, когда воркер перейдёт на 5 аргументов (Б-83).
   ``app_couple_feed_page`` — последний столбец ``withheld_crisis`` (только автору).

3. **Отказ модели и сбой записи** (D1). Завершения по токену идемпотентны: повтор той же записи
   после неудачной (не зафиксированной) — проходит; после зафиксированной — ``false``/``lost``
   без изменений; истёкшая, но не перехваченная аренда завершается. Поэтому защита — повтор
   записи исхода воркером (A3) до успеха или ``lost``; в БД «отказ» иначе не записать (запись
   и есть отметка). Отказ не снимает аренду досрочно (``pipeline_release`` для отказа не
   вызывается), так что до истечения аренды сообщение никому не выдаётся.

4. **Старые функции без аренды** (MINOR-8, Б-83): ``pipeline_original``, ``pipeline_deliver``,
   ``pipeline_withhold`` больше не трогают сообщение, у которого есть токен аренды (оно у
   воркера) — обхода «ровно один раз» нет. EXECUTE у ``feelings_worker`` пока оставлен: ими
   под этой ролью готовят данные тесты 11 файлов (в т.ч. A8); план — Б-83.

5. **Кризис после распада пары** (О-3): остановка безопасностью сильнее — ``safety`` (с типом),
   а не ``couple_ended``: автору нужна помощь. Захват распавшейся пары до вызова модели —
   ``couple_ended`` (модель не вызывается; текст проверялся при создании черновика, АР-40).

Downgrade: функции 0014 удаляются; ``pipeline_claim``, ``pipeline_withhold_leased`` (4 арг.),
``pipeline_withhold`` — тела 0013, ``pipeline_deliver`` — 0005, ``pipeline_original`` — 0001,
``app_couple_feed_page`` — 0013; отложенные сроки без токена сбрасываются, CHECK аренды и колонка
типа кризиса — как в 0013.
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rev(name: str) -> ModuleType:
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(f"_rev_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"не найдена ревизия: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R13 = _rev("0013_pipeline_worker.py")
MAX_WAIT = "interval '24 hours'"  # АР-45: дольше — attempts_exhausted

SCHEMA = (
    "ALTER TABLE partner_messages ADD COLUMN withheld_crisis safety_event_type",
    "ALTER TABLE partner_messages ADD CONSTRAINT ck_partner_messages_withheld_crisis_safety "
    "CHECK (withheld_crisis IS NULL OR (withheld_reason = 'safety' "
    "AND withheld_crisis <> 'underage'))",
    "ALTER TABLE partner_messages DROP CONSTRAINT ck_partner_messages_work_lease_whole",
    "ALTER TABLE partner_messages ADD CONSTRAINT ck_partner_messages_work_lease_has_until "
    "CHECK (work_lease_token IS NULL OR work_lease_until IS NOT NULL)",
)

DROP_SCHEMA = (
    "UPDATE partner_messages SET work_lease_until = NULL WHERE work_lease_token IS NULL "
    "AND work_lease_until IS NOT NULL",
    "ALTER TABLE partner_messages DROP CONSTRAINT IF EXISTS ck_partner_messages_work_lease_has_until",
    "ALTER TABLE partner_messages ADD CONSTRAINT ck_partner_messages_work_lease_whole "
    "CHECK ((work_lease_token IS NULL) = (work_lease_until IS NULL))",
    "ALTER TABLE partner_messages DROP CONSTRAINT IF EXISTS "
    "ck_partner_messages_withheld_crisis_safety",
    "ALTER TABLE partner_messages DROP COLUMN IF EXISTS withheld_crisis",
)


def _fill(template: str, lost: str = "'lost'") -> str:
    return R13._fill(template.replace("@MAX_WAIT@", MAX_WAIT), lost)


CLAIM = _fill(
    r"""
CREATE OR REPLACE FUNCTION pipeline_claim(p_lease interval, p_max_attempts integer,
                                          p_limit integer)
    RETURNS TABLE (message_id uuid, token uuid, attempts integer)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    #variable_conflict use_column
    DECLARE
        v_lim integer := LEAST(GREATEST(COALESCE(p_limit, 1), 1), 100);
        r record;
    BEGIN@ARG_CHECKS@
        -- Свободные: аренды нет и срок «не раньше» (pipeline_release) прошёл, или аренда
        -- истекла (воркер упал). Ничего не ждёт: SKIP LOCKED, пара и пользователи не блокируются.
        FOR r IN
            SELECT m.id, m.work_attempts AS n,
                   (c.status = 'active' AND m.recipient_id IS NOT NULL) AS live,
                   m.sent_at < now() - @MAX_WAIT@ AS stale
              FROM public.partner_messages m
              JOIN public.couples c ON c.id = m.couple_id
             WHERE m.status = 'processing'
               AND (m.work_lease_until IS NULL OR m.work_lease_until < now())
             ORDER BY m.sent_at, m.id
             LIMIT v_lim
             FOR NO KEY UPDATE OF m SKIP LOCKED
        LOOP
            IF NOT r.live OR r.stale OR r.n >= p_max_attempts THEN
                UPDATE public.partner_messages m
                   SET status = 'withheld',
                       withheld_reason = CASE WHEN r.live THEN 'attempts_exhausted'
                                              ELSE 'couple_ended' END,
                       @RELEASE@
                 WHERE m.id = r.id;
                CONTINUE;
            END IF;
            message_id := r.id;
            token := gen_random_uuid();
            UPDATE public.partner_messages m
               SET work_lease_token = token, work_lease_until = now() + p_lease,
                   work_attempts = m.work_attempts + 1
             WHERE m.id = r.id
            RETURNING m.work_attempts INTO attempts;
            RETURN NEXT;
        END LOOP;
    END $f$
"""
)

RELEASE_FN = r"""
CREATE FUNCTION pipeline_release(p_message uuid, p_token uuid, p_retry_after interval,
                                 p_count_attempt boolean) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    BEGIN
        IF p_count_attempt IS NULL OR (p_retry_after IS NOT NULL
           AND (p_retry_after < interval '0' OR p_retry_after > interval '1 day')) THEN
            RAISE EXCEPTION 'bad release' USING ERRCODE = '22023';
        END IF;
        -- Одна строка сообщения, без других блокировок (АР-13). Срок без токена — «не раньше».
        UPDATE public.partner_messages m
           SET work_lease_token = NULL,
               work_lease_until = CASE WHEN COALESCE(p_retry_after, interval '0') > interval '0'
                                       THEN now() + p_retry_after END,
               work_attempts = CASE WHEN p_count_attempt THEN m.work_attempts
                                    ELSE GREATEST(m.work_attempts - 1, 0) END
         WHERE m.id = p_message AND m.status = 'processing'
           AND p_token IS NOT NULL AND m.work_lease_token = p_token;
        RETURN FOUND;
    END $f$
"""

WITHHOLD5 = _fill(
    r"""
CREATE FUNCTION pipeline_withhold_leased(p_message uuid, p_token uuid, p_reason varchar,
                                         p_events safety_event_type[],
                                         p_crisis safety_event_type) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE
        v_author uuid; v_couple uuid; v_couple_status public.couple_status;
        v_recipient uuid; v_reply uuid;
    BEGIN
        IF p_reason IS NULL OR p_reason NOT IN @WORKER_REASONS@
           OR (p_reason = 'safety') <> (p_crisis IS NOT NULL)
           OR p_crisis = 'underage' THEN
            RAISE EXCEPTION 'bad reason' USING ERRCODE = '22023';
        END IF;@LOCK_LEASED@
        -- Кризис сильнее распада пары (О-3): причина safety и тип — для помощи автору (§9.3).
        UPDATE public.partner_messages
           SET status = 'withheld', withheld_reason = p_reason, withheld_crisis = p_crisis,
               @RELEASE@
         WHERE id = p_message;@EVENTS@
        RETURN true;
    END $f$
""",
    lost="false",
)

# Обёртка прежней сигнатуры (0013): для safety главный тип — первый из p_events.
WITHHOLD4 = r"""
CREATE OR REPLACE FUNCTION pipeline_withhold_leased(p_message uuid, p_token uuid,
                                                    p_reason varchar,
                                                    p_events safety_event_type[])
    RETURNS boolean
    LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    SELECT public.pipeline_withhold_leased(
        p_message, p_token, p_reason, p_events,
        CASE WHEN p_reason = 'safety' THEN
            (SELECT e FROM unnest(p_events) WITH ORDINALITY AS t(e, i)
              WHERE e IS NOT NULL AND e <> 'underage' ORDER BY i LIMIT 1)
        END) $f$
"""

# Старые функции (0001/0005/0013) — без вмешательства в сообщения под арендой воркера.
OLD_ORIGINAL = r"""
CREATE OR REPLACE FUNCTION pipeline_original(p_message uuid) RETURNS bytea
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$ SELECT original_enc FROM public.partner_messages
           WHERE id = p_message AND status = 'processing' AND work_lease_token IS NULL $f$
"""

_LEASE_GUARD = "AND status = 'processing'"
OLD_DELIVER = _rev("0005_account_delete.py").PIPELINE_DELIVER.replace(
    _LEASE_GUARD, _LEASE_GUARD + " AND work_lease_token IS NULL", 1
)
OLD_WITHHOLD = R13.WITHHOLD_0005_SAFETY.replace(
    _LEASE_GUARD, _LEASE_GUARD + " AND work_lease_token IS NULL", 1
)
for _sql in (OLD_DELIVER, OLD_WITHHOLD):
    if "work_lease_token IS NULL" not in _sql:
        raise RuntimeError("тело старой функции конвейера не совпадает с ожидаемым")

FEED_PAGE = R13.FEED_PAGE.replace(
    "                   withheld_reason varchar)",
    "                   withheld_reason varchar, withheld_crisis safety_event_type)",
    1,
).replace(
    "               CASE WHEN m.author_id = v_uid THEN m.withheld_reason END\n",
    "               CASE WHEN m.author_id = v_uid THEN m.withheld_reason END,\n"
    "               CASE WHEN m.author_id = v_uid THEN m.withheld_crisis END\n",
    1,
)
if FEED_PAGE.count("withheld_crisis") != 2:
    raise RuntimeError("тело app_couple_feed_page 0013 не совпадает с ожидаемым")

NEW_SIGNATURES = (
    "pipeline_release(uuid, uuid, interval, boolean)",
    "pipeline_withhold_leased(uuid, uuid, character varying, safety_event_type[], "
    "safety_event_type)",
)


def upgrade() -> None:
    for stmt in SCHEMA:
        op.execute(stmt)
    for sql in (CLAIM, RELEASE_FN, WITHHOLD5, WITHHOLD4, OLD_ORIGINAL, OLD_DELIVER, OLD_WITHHOLD):
        op.execute(sql)
    for sig in NEW_SIGNATURES:
        op.execute(f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO feelings_worker")
    R13._feed(FEED_PAGE)


def downgrade() -> None:
    R13._feed(R13.FEED_PAGE)
    op.execute(R13.WITHHOLD.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
    for sig in NEW_SIGNATURES:
        op.execute(f"DROP FUNCTION IF EXISTS {sig}")
    op.execute(R13.CLAIM.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
    op.execute(R13.WITHHOLD_0005_SAFETY)
    op.execute(_rev("0005_account_delete.py").PIPELINE_DELIVER)
    op.execute(_rev("0005_account_delete.py")._func_0001("pipeline_original"))
    for stmt in DROP_SCHEMA:
        op.execute(stmt)

"""Воркер конвейера входящего: захват с арендой, доставка по токену, очередь уведомлений (3.2, A1).

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-23 20:00:00

ФТ-09; ТЗ §2 п.2–3, п.7, §6.2 п.3–7, §7, §9.3, §10; DECISIONS.md АР-11, АР-13, АР-23, АР-27,
АР-28, АР-36, АР-38, АР-44; BACKLOG Б-39, Б-44, Б-45, Б-47, Б-48.

1. **Служебные колонки ``partner_messages``** (не новые таблицы: §7 — 17 таблиц; как АР-36):

   - ``withheld_reason varchar(32)`` — почему сообщение не доставлено (видит только автор):
     ``safety`` (кризис, АР-28), ``model_refusal`` (отказ модели, АР-20/27), ``pipeline_error``
     (неустранимая ошибка конвейера — настройка и т.п., АР-27), ``attempts_exhausted`` (исчерпан
     предел попыток), ``couple_ended`` (пара распалась или получатель удалил аккаунт до доставки,
     ТЗ §4.5 п.2). CHECK: только эти коды и только у ``withheld``. Прежние ``withheld`` →
     ``safety`` (до 0013 их ставила только безопасность);
   - ``work_lease_token``/``work_lease_until``/``work_attempts`` — аренда сообщения одной копией
     воркера: в ``processing`` — конвейер, в ``delivered`` — уведомление получателю (фазы не
     пересекаются, завершение конвейера снимает аренду и обнуляет счётчик). CHECK: токен и срок —
     вместе; счётчик ≥ 0;
   - ``notify_done_at`` — уведомление получателю отправлено или больше не нужно. Очередь
     уведомлений (Б-47) — ``delivered`` без ``notify_done_at``: id сообщения и ``recipient_id``,
     без текста. Прежние доставленные помечаются уведомлёнными (не слать задним числом).

   Индексы очередей: ``ix_partner_messages_processing (sent_at, id) WHERE status = 'processing'``,
   ``ix_partner_messages_notify (delivered_at, id) WHERE status = 'delivered' AND notify_done_at
   IS NULL``. У ``feelings_app`` прав на новые колонки нет (права на ``partner_messages`` —
   по колонкам, 0001).

2. **Функции ``feelings_worker``** (SECURITY DEFINER; текстов не отдают, кроме шифротекста
   оригинала по своей аренде):

   - ``pipeline_claim(p_lease interval, p_max_attempts integer, p_limit integer) RETURNS TABLE
     (message_id uuid, token uuid, attempts integer)`` — ``processing`` со свободной или истёкшей
     арендой (догон после перезапуска), по ``(sent_at, id)``; строки ``FOR NO KEY UPDATE SKIP
     LOCKED`` — ничего не ждёт (с удалением аккаунта, выходом, второй копией воркера не
     взаимоблокируется; пару и пользователей не блокирует). Без модели завершает: пара не
     ``active`` или получателя нет — ``withheld/couple_ended``; ``work_attempts ≥ p_max_attempts`` —
     ``withheld/attempts_exhausted``. Остальным — новый токен, срок, ``attempts + 1``.
     22023 — аренда вне (0, 1 день], предел попыток вне 1–100; ``p_limit`` 1–100;
   - ``pipeline_read(p_message uuid, p_token uuid) RETURNS TABLE (original_enc bytea, draft_id
     uuid)`` — Б-39, Б-48: строка только если сообщение ``processing``, токен свой, черновик
     сообщения существует и ``sent`` (одобрен автором «Отправить», ``app_send_draft``); иначе
     пусто — ``approve()`` не вызывать;
   - ``pipeline_deliver_leased(p_message, p_token, p_for_recipient bytea, p_is_adapted boolean,
     p_tone smallint, p_harsh boolean, p_events safety_event_type[]) RETURNS text`` —
     ``delivered``; ``couple_ended`` (пара не ``active`` или получателя нет — не доставлено,
     ``withheld/couple_ended``); ``lost`` (токен не свой, не ``processing``, сообщения нет — ничего
     не изменено). Ответ — исходное ``answered`` (как 0005). 22023 — ``p_for_recipient`` NULL;
   - ``pipeline_withhold_leased(p_message, p_token, p_reason varchar, p_events
     safety_event_type[]) RETURNS boolean`` — ``withheld`` с причиной ``safety``,
     ``model_refusal`` или ``pipeline_error`` (иначе 22023); ``false`` — как ``lost``;
   - события безопасности ``p_events`` (оба завершения) — строки ``safety_events`` от имени
     **автора**, только тип (NULL/пусто — нет; повторы — по одной строке); пишутся только если
     завершение состоялось;
   - ``pipeline_claim_notify(p_lease, p_max_attempts, p_limit) RETURNS TABLE (message_id uuid,
     recipient_id uuid, token uuid, attempts integer)`` — для 3.5: ``delivered`` без
     ``notify_done_at``, свободная/истёкшая аренда, по ``(delivered_at, id)``, ``SKIP LOCKED``;
     пара не ``active``, получателя нет или он помечен удалённым, попытки исчерпаны — отметка
     ``notify_done_at`` без выдачи; ``pipeline_notify_done(p_message, p_token) RETURNS boolean``
     — отметка и снятие аренды по своему токену (прочитанное до отметки — тоже).

   Порядок блокировок завершений (АР-13, АР-38, Б-45): строка **автора** в ``users``
   ``FOR KEY SHARE`` → пара ``FOR KEY SHARE`` → сообщение (``FOR NO KEY UPDATE``) → исходное
   при ответе. Строка автора первой — потому что вставка ``safety_events`` берёт её ``FOR KEY
   SHARE`` (FK); после пары это шло бы против ``app_delete_account`` автора (пользователь →
   пара, 40P01). Каждая строка сообщения меняется один раз (АР-13: повторное изменение своей
   строки перепроверяет FK). Истёкшая, но не перехваченная аренда завершается.

3. ``pipeline_withhold`` (0005) — та же функция, но ставит ``withheld_reason = 'safety'`` и
   снимает аренду (иначе CHECK причины). ``pipeline_original``/``pipeline_deliver`` (0001/0005)
   не меняются: воркер 3.2 ими не пользуется (нет проверки аренды) — остаются для тестовых
   данных и совместимости, снятие — BACKLOG.

4. ``app_couple_feed_page`` (0012) — та же сигнатура, в выдаче добавлен последний столбец
   ``withheld_reason`` (автору; получатель ``withheld`` не видит). Тип результата меняется —
   функция пересоздаётся, права те же.

Downgrade: функции 0013 удаляются, ``pipeline_withhold`` — тело 0005, ``app_couple_feed_page`` —
тело 0012, колонки, CHECK и индексы 0013 удаляются (причины остановки и отметки уведомлений
теряются).
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
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


REASONS = "('safety', 'model_refusal', 'pipeline_error', 'attempts_exhausted', 'couple_ended')"
WORKER_REASONS = "('safety', 'model_refusal', 'pipeline_error')"

SCHEMA = (
    "ALTER TABLE partner_messages ADD COLUMN withheld_reason varchar(32)",
    "ALTER TABLE partner_messages ADD COLUMN work_lease_token uuid",
    "ALTER TABLE partner_messages ADD COLUMN work_lease_until timestamptz",
    "ALTER TABLE partner_messages ADD COLUMN work_attempts integer DEFAULT 0 NOT NULL",
    "ALTER TABLE partner_messages ADD COLUMN notify_done_at timestamptz",
    # До 0013 withheld ставила только безопасность; доставленные — уже не уведомлять.
    "UPDATE partner_messages SET withheld_reason = 'safety' WHERE status = 'withheld'",
    "UPDATE partner_messages SET notify_done_at = COALESCE(delivered_at, sent_at) "
    "WHERE status IN ('delivered', 'read', 'answered')",
    "ALTER TABLE partner_messages ADD CONSTRAINT ck_partner_messages_withheld_reason_code "
    f"CHECK (withheld_reason IS NULL OR withheld_reason IN {REASONS})",
    "ALTER TABLE partner_messages ADD CONSTRAINT ck_partner_messages_withheld_reason_status "
    "CHECK (withheld_reason IS NULL OR status = 'withheld')",
    "ALTER TABLE partner_messages ADD CONSTRAINT ck_partner_messages_work_lease_whole "
    "CHECK ((work_lease_token IS NULL) = (work_lease_until IS NULL))",
    "ALTER TABLE partner_messages ADD CONSTRAINT ck_partner_messages_work_attempts_nonnegative "
    "CHECK (work_attempts >= 0)",
    "CREATE INDEX ix_partner_messages_processing ON partner_messages (sent_at, id) "
    "WHERE status = 'processing'",
    "CREATE INDEX ix_partner_messages_notify ON partner_messages (delivered_at, id) "
    "WHERE status = 'delivered' AND notify_done_at IS NULL",
)

DROP_SCHEMA = (
    "DROP INDEX IF EXISTS ix_partner_messages_notify",
    "DROP INDEX IF EXISTS ix_partner_messages_processing",
    "ALTER TABLE partner_messages DROP CONSTRAINT IF EXISTS "
    "ck_partner_messages_work_attempts_nonnegative",
    "ALTER TABLE partner_messages DROP CONSTRAINT IF EXISTS ck_partner_messages_work_lease_whole",
    "ALTER TABLE partner_messages DROP CONSTRAINT IF EXISTS "
    "ck_partner_messages_withheld_reason_status",
    "ALTER TABLE partner_messages DROP CONSTRAINT IF EXISTS "
    "ck_partner_messages_withheld_reason_code",
    "ALTER TABLE partner_messages DROP COLUMN IF EXISTS notify_done_at",
    "ALTER TABLE partner_messages DROP COLUMN IF EXISTS work_attempts",
    "ALTER TABLE partner_messages DROP COLUMN IF EXISTS work_lease_until",
    "ALTER TABLE partner_messages DROP COLUMN IF EXISTS work_lease_token",
    "ALTER TABLE partner_messages DROP COLUMN IF EXISTS withheld_reason",
)

ARG_CHECKS = """
        IF p_lease IS NULL OR p_lease <= interval '0' OR p_lease > interval '1 day' THEN
            RAISE EXCEPTION 'bad lease' USING ERRCODE = '22023';
        END IF;
        IF p_max_attempts IS NULL OR p_max_attempts < 1 OR p_max_attempts > 100 THEN
            RAISE EXCEPTION 'bad max attempts' USING ERRCODE = '22023';
        END IF;"""

# Завершение по аренде: автор (FOR KEY SHARE) → пара (FOR KEY SHARE) → сообщение. Автор и пара
# читаются без блокировки, блокируются, затем сообщение перечитывается под блокировкой: удалено
# вместе с аккаунтом автора или аренду перехватили — ничего не меняется.
LOCK_LEASED = """
        SELECT m.author_id, m.couple_id INTO v_author, v_couple
          FROM public.partner_messages m WHERE m.id = p_message;
        IF NOT FOUND OR p_token IS NULL THEN RETURN @LOST@; END IF;
        PERFORM 1 FROM public.users u WHERE u.id = v_author FOR KEY SHARE;
        IF NOT FOUND THEN RETURN @LOST@; END IF;
        SELECT c.status INTO v_couple_status FROM public.couples c
         WHERE c.id = v_couple FOR KEY SHARE;
        IF NOT FOUND THEN RETURN @LOST@; END IF;
        SELECT m.recipient_id, m.reply_to_id INTO v_recipient, v_reply
          FROM public.partner_messages m
         WHERE m.id = p_message AND m.status = 'processing' AND m.work_lease_token = p_token
           FOR NO KEY UPDATE;
        IF NOT FOUND THEN RETURN @LOST@; END IF;"""

# События безопасности от имени автора — только тип (ТЗ §9.3); строка автора уже под KEY SHARE.
EVENTS = """
        INSERT INTO public.safety_events (user_id, type)
        SELECT v_author, e FROM unnest(COALESCE(p_events, '{}')) AS e WHERE e IS NOT NULL;"""

RELEASE = "work_lease_token = NULL, work_lease_until = NULL, work_attempts = 0"


def _fill(template: str, lost: str = "'lost'") -> str:
    for name, value in (
        ("ARG_CHECKS", ARG_CHECKS),
        ("LOCK_LEASED", LOCK_LEASED),
        ("EVENTS", EVENTS),
        ("RELEASE", RELEASE),
        ("REASONS", REASONS),
        ("WORKER_REASONS", WORKER_REASONS),
    ):
        template = template.replace(f"@{name}@", value)
    return template.replace("@LOST@", lost)


CLAIM = _fill(
    r"""
CREATE FUNCTION pipeline_claim(p_lease interval, p_max_attempts integer, p_limit integer)
    RETURNS TABLE (message_id uuid, token uuid, attempts integer)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    #variable_conflict use_column
    DECLARE
        v_lim integer := LEAST(GREATEST(COALESCE(p_limit, 1), 1), 100);
        r record;
    BEGIN@ARG_CHECKS@
        -- Новые и брошенные (аренда истекла — воркер упал) сообщения, старые первыми. Строки,
        -- занятые другой копией воркера, удалением аккаунта или выходом, пропускаются: захват
        -- ничего не ждёт, поэтому пару и пользователей не блокирует (состояние пары читается
        -- без блокировки: распад необратим; гонку с идущим распадом решит завершение).
        FOR r IN
            SELECT m.id, m.work_attempts AS n,
                   (c.status = 'active' AND m.recipient_id IS NOT NULL) AS live
              FROM public.partner_messages m
              JOIN public.couples c ON c.id = m.couple_id
             WHERE m.status = 'processing'
               AND (m.work_lease_token IS NULL OR m.work_lease_until < now())
             ORDER BY m.sent_at, m.id
             LIMIT v_lim
             FOR NO KEY UPDATE OF m SKIP LOCKED
        LOOP
            IF NOT r.live OR r.n >= p_max_attempts THEN
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

READ = r"""
CREATE FUNCTION pipeline_read(p_message uuid, p_token uuid)
    RETURNS TABLE (original_enc bytea, draft_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    -- Б-48: одобрение — это статус в БД: сообщение ещё в конвейере, аренда своя, черновик
    -- существует и отправлен автором («Отправить», app_send_draft). Б-39: draft_id — AAD.
    SELECT m.original_enc, m.draft_id
      FROM public.partner_messages m
      JOIN public.message_drafts d ON d.id = m.draft_id AND d.status = 'sent'
     WHERE m.id = p_message AND m.status = 'processing'
       AND p_token IS NOT NULL AND m.work_lease_token = p_token $f$
"""

DELIVER = _fill(
    r"""
CREATE FUNCTION pipeline_deliver_leased(p_message uuid, p_token uuid, p_for_recipient bytea,
                                        p_is_adapted boolean, p_tone smallint, p_harsh boolean,
                                        p_events safety_event_type[]) RETURNS text
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE
        v_author uuid; v_couple uuid; v_couple_status public.couple_status;
        v_recipient uuid; v_reply uuid;
    BEGIN
        IF p_for_recipient IS NULL THEN
            RAISE EXCEPTION 'no text for recipient' USING ERRCODE = '22023';
        END IF;@LOCK_LEASED@
        -- Пара распалась или получатель удалил аккаунт, пока шла модель: обмен прекращён
        -- (ТЗ §4.5 п.2) — не доставляется, автору видна причина.
        IF v_couple_status <> 'active' OR v_recipient IS NULL THEN
            UPDATE public.partner_messages
               SET status = 'withheld', withheld_reason = 'couple_ended', @RELEASE@
             WHERE id = p_message;@EVENTS@
            RETURN 'couple_ended';
        END IF;
        UPDATE public.partner_messages
           SET adapted_enc = p_for_recipient, is_adapted = p_is_adapted, tone_score = p_tone,
               harsh_tone_warning = p_harsh, status = 'delivered', delivered_at = now(),
               @RELEASE@
         WHERE id = p_message;
        IF v_reply IS NOT NULL THEN
            UPDATE public.partner_messages SET status = 'answered', answered_at = now()
             WHERE id = v_reply AND status IN ('delivered', 'read');
        END IF;@EVENTS@
        RETURN 'delivered';
    END $f$
"""
)

WITHHOLD = _fill(
    r"""
CREATE FUNCTION pipeline_withhold_leased(p_message uuid, p_token uuid, p_reason varchar,
                                         p_events safety_event_type[]) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE
        v_author uuid; v_couple uuid; v_couple_status public.couple_status;
        v_recipient uuid; v_reply uuid;
    BEGIN
        IF p_reason IS NULL OR p_reason NOT IN @WORKER_REASONS@ THEN
            RAISE EXCEPTION 'bad reason' USING ERRCODE = '22023';
        END IF;@LOCK_LEASED@
        UPDATE public.partner_messages
           SET status = 'withheld', withheld_reason = p_reason, @RELEASE@
         WHERE id = p_message;@EVENTS@
        RETURN true;
    END $f$
""",
    lost="false",
)

CLAIM_NOTIFY = _fill(
    r"""
CREATE FUNCTION pipeline_claim_notify(p_lease interval, p_max_attempts integer, p_limit integer)
    RETURNS TABLE (message_id uuid, recipient_id uuid, token uuid, attempts integer)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    #variable_conflict use_column
    DECLARE
        v_lim integer := LEAST(GREATEST(COALESCE(p_limit, 1), 1), 100);
        r record;
    BEGIN@ARG_CHECKS@
        -- Б-47: в очереди — id сообщения и получатель, текста нет. Ничего не ждёт (SKIP LOCKED).
        FOR r IN
            SELECT m.id, m.recipient_id AS rid, m.work_attempts AS n,
                   (c.status = 'active' AND m.recipient_id IS NOT NULL
                    AND u.deleted_at IS NULL) AS live
              FROM public.partner_messages m
              JOIN public.couples c ON c.id = m.couple_id
              LEFT JOIN public.users u ON u.id = m.recipient_id
             WHERE m.status = 'delivered' AND m.notify_done_at IS NULL
               AND (m.work_lease_token IS NULL OR m.work_lease_until < now())
             ORDER BY m.delivered_at, m.id
             LIMIT v_lim
             FOR NO KEY UPDATE OF m SKIP LOCKED
        LOOP
            IF NOT r.live OR r.n >= p_max_attempts THEN
                -- Пара распалась, получатель удалён или попытки исчерпаны — не уведомлять.
                UPDATE public.partner_messages m SET notify_done_at = now(), @RELEASE@
                 WHERE m.id = r.id;
                CONTINUE;
            END IF;
            message_id := r.id;
            recipient_id := r.rid;
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

NOTIFY_DONE = _fill(
    r"""
CREATE FUNCTION pipeline_notify_done(p_message uuid, p_token uuid) RETURNS boolean
    LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    WITH u AS (
        UPDATE public.partner_messages SET notify_done_at = now(), @RELEASE@
         WHERE id = p_message AND p_token IS NOT NULL AND work_lease_token = p_token
           AND notify_done_at IS NULL AND status IN ('delivered', 'read', 'answered')
        RETURNING 1)
    SELECT EXISTS (SELECT 1 FROM u) $f$
"""
)

# 0005 + причина: прежняя функция остаётся (тестовые данные), но не нарушает CHECK причины.
WITHHOLD_0005_SAFETY = _fill(
    r"""
CREATE OR REPLACE FUNCTION pipeline_withhold(p_message uuid) RETURNS boolean
    LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    SELECT 1 FROM public.couples c
     WHERE c.id = (SELECT m.couple_id FROM public.partner_messages m WHERE m.id = p_message)
       FOR KEY SHARE;
    WITH u AS (
        UPDATE public.partner_messages
           SET status = 'withheld', withheld_reason = 'safety', @RELEASE@
         WHERE id = p_message AND status = 'processing'
        RETURNING 1)
    SELECT EXISTS (SELECT 1 FROM u) $f$
"""
)

FEED_SIGNATURE = "app_couple_feed_page(integer, timestamp with time zone, uuid)"

FEED_PAGE = r"""
CREATE FUNCTION app_couple_feed_page(p_limit integer, p_before_at timestamptz, p_before_id uuid)
    RETURNS TABLE (id uuid, couple_id uuid, outgoing boolean, text_enc bytea, is_adapted boolean,
                   harsh_tone_warning boolean, status partner_message_status,
                   reply_to_id uuid, draft_id uuid, sent_at timestamptz, read_at timestamptz,
                   withheld_reason varchar)
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    #variable_conflict use_column
    DECLARE
        v_uid uuid := public.app_uid();
        v_lim integer := LEAST(GREATEST(COALESCE(p_limit, 1), 1), 100);
        v_couple uuid;
    BEGIN
        IF v_uid IS NULL THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        PERFORM 1 FROM public.users u WHERE u.id = v_uid AND u.deleted_at IS NULL;
        IF NOT FOUND THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        IF (p_before_at IS NULL) <> (p_before_id IS NULL) THEN
            RAISE EXCEPTION 'bad cursor' USING ERRCODE = '22023';
        END IF;
        SELECT cm.couple_id INTO v_couple FROM public.couple_members cm
          JOIN public.couples c ON c.id = cm.couple_id
         WHERE cm.user_id = v_uid AND cm.active AND c.status = 'active';
        IF NOT FOUND THEN RETURN; END IF;
        RETURN QUERY
        SELECT m.id, m.couple_id, m.author_id = v_uid,
               CASE WHEN m.author_id = v_uid THEN m.original_enc ELSE m.adapted_enc END,
               m.is_adapted, m.harsh_tone_warning, m.status, m.reply_to_id, m.draft_id,
               m.sent_at, m.read_at,
               CASE WHEN m.author_id = v_uid THEN m.withheld_reason END
          FROM public.partner_messages m
         WHERE m.couple_id = v_couple
           AND (m.author_id = v_uid
                OR (m.recipient_id = v_uid AND m.status NOT IN ('processing', 'withheld')))
           AND (p_before_at IS NULL OR (m.sent_at, m.id) < (p_before_at, p_before_id))
         ORDER BY m.sent_at DESC, m.id DESC
         LIMIT v_lim;
    END $f$
"""

WORKER_SIGNATURES = (
    "pipeline_claim(interval, integer, integer)",
    "pipeline_read(uuid, uuid)",
    "pipeline_deliver_leased(uuid, uuid, bytea, boolean, smallint, boolean, safety_event_type[])",
    "pipeline_withhold_leased(uuid, uuid, character varying, safety_event_type[])",
    "pipeline_claim_notify(interval, integer, integer)",
    "pipeline_notify_done(uuid, uuid)",
)


def _feed(sql: str) -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {FEED_SIGNATURE}")
    op.execute(sql)
    op.execute(f"REVOKE ALL ON FUNCTION {FEED_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FEED_SIGNATURE} TO feelings_app")


def upgrade() -> None:
    # По одной команде: asyncpg не выполняет несколько команд в одном запросе.
    for stmt in SCHEMA:
        op.execute(stmt)
    for sql in (CLAIM, READ, DELIVER, WITHHOLD, CLAIM_NOTIFY, NOTIFY_DONE):
        op.execute(sql)
    for sig in WORKER_SIGNATURES:
        op.execute(f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO feelings_worker")
    op.execute(WITHHOLD_0005_SAFETY)
    _feed(FEED_PAGE)


def downgrade() -> None:
    _feed(_rev("0012_couple_feed_page.py").FEED_PAGE)
    op.execute(_rev("0005_account_delete.py").PIPELINE_WITHHOLD)
    for sig in WORKER_SIGNATURES:
        op.execute(f"DROP FUNCTION IF EXISTS {sig}")
    for stmt in DROP_SCHEMA:
        op.execute(stmt)

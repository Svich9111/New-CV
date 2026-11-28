"""Набор признаков кризиса у остановленного сообщения — протокол §9.3 автору целиком (A1).

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-28 12:00:00

ТЗ §7, §9.3; ФТ-11; DECISIONS.md АР-28, АР-45 (дополнение 3.4), АР-13, АР-38; задача 3.4
(MAJOR-1 р.1, предложение A1 № 5, вопрос 6); ревью 3.4 р.2 MINOR-A; BACKLOG Б-103.

До 0021 у сообщения хранился только главный тип (``withheld_crisis``), а блоки ответа автору
``crisis.respond`` зависят от **всего** набора признаков (правила «А»/«Б»): клиент показывал
консервативный набор. Теперь хранится и набор, а лента отдаёт автору имена блоков, вычисленные
на чтении тем же ``crisis.plan`` (одна копия правил — на сервере).

1. Колонка ``partner_messages.withheld_crisis_types safety_event_type[]`` — **только типы**,
   без текста сообщения (§7; журналы и события без текстов — правило не задето). CHECK
   ``withheld_crisis_types_safety``: NULL или — главный тип задан (а он только у ``safety``,
   CHECK 0014), набор одномерный, 1–4 элемента, без NULL и без ``underage`` (у сообщения не
   строка конвейера), главный тип входит в набор. NULL — набор неизвестен (сообщения до 0021):
   клиент показывает консервативный набор (3.4, «Заметки A4 (раунд 2)»).
2. ``pipeline_withhold_leased(p_message, p_token, p_reason, p_events, p_crisis, p_crisis_types
   safety_event_type[] DEFAULT NULL) RETURNS boolean`` — **заменяет** 5-аргументную 0014 (одна
   функция остановки у воркера; вызов с 5 аргументами — набор NULL). SECURITY DEFINER,
   ``search_path = public, pg_temp``, EXECUTE только ``feelings_worker`` (как 0014). Проверки
   0014 и ещё: набор — только при ``safety`` (иначе 22023), непустой, одномерный, без NULL и
   ``underage``, содержит ``p_crisis`` (иначе 22023; NULL — набор неизвестен). Записывается без повторов, по порядку
   перечисления. Блокировки — как 0013/0014 (АР-13, АР-38, Б-45): автор ``FOR KEY SHARE`` →
   пара ``FOR KEY SHARE`` → сообщение ``FOR NO KEY UPDATE``; строка сообщения меняется один раз.
3. ``app_couple_feed_page`` — последний столбец ``withheld_crisis_types`` (только автору, как
   ``withheld_crisis``; получателю NULL). Тип результата меняется — функция пересоздаётся, права
   те же (EXECUTE только ``feelings_app``).

RLS и права таблицы не меняются (запись — только функциями воркера, чтение — функцией ленты).

Downgrade: 6-аргументная функция удаляется, 5-аргументная (с правами 0014: ``REVOKE ALL … FROM
PUBLIC``, ``GRANT EXECUTE … TO feelings_worker``) и лента — тела 0014, CHECK и колонка удаляются
(набор признаков теряется, главный тип остаётся).
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
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
R14 = _rev("0014_pipeline_release_crisis.py")

TYPES_CHECK = (
    "withheld_crisis_types IS NULL OR (withheld_crisis IS NOT NULL "
    "AND array_ndims(withheld_crisis_types) = 1 "
    "AND cardinality(withheld_crisis_types) BETWEEN 1 AND 4 "
    "AND array_position(withheld_crisis_types, NULL) IS NULL "
    "AND array_position(withheld_crisis_types, 'underage') IS NULL "
    "AND withheld_crisis = ANY (withheld_crisis_types))"
)

SCHEMA = (
    "ALTER TABLE partner_messages ADD COLUMN withheld_crisis_types safety_event_type[]",
    "ALTER TABLE partner_messages ADD CONSTRAINT ck_partner_messages_withheld_crisis_types_safety "
    f"CHECK ({TYPES_CHECK})",
)

DROP_SCHEMA = (
    "ALTER TABLE partner_messages DROP CONSTRAINT IF EXISTS "
    "ck_partner_messages_withheld_crisis_types_safety",
    "ALTER TABLE partner_messages DROP COLUMN IF EXISTS withheld_crisis_types",
)

WITHHOLD6 = R14._fill(
    r"""
CREATE FUNCTION pipeline_withhold_leased(p_message uuid, p_token uuid, p_reason varchar,
                                         p_events safety_event_type[],
                                         p_crisis safety_event_type,
                                         p_crisis_types safety_event_type[] DEFAULT NULL)
    RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE
        v_author uuid; v_couple uuid; v_couple_status public.couple_status;
        v_recipient uuid; v_reply uuid; v_types public.safety_event_type[];
    BEGIN
        IF p_reason IS NULL OR p_reason NOT IN @WORKER_REASONS@
           OR (p_reason = 'safety') <> (p_crisis IS NOT NULL)
           OR p_crisis = 'underage' THEN
            RAISE EXCEPTION 'bad reason' USING ERRCODE = '22023';
        END IF;
        -- Набор признаков (0021): только типы, только при safety, главный тип — в наборе.
        IF p_crisis_types IS NOT NULL THEN
            IF p_reason <> 'safety' OR cardinality(p_crisis_types) = 0
               OR array_ndims(p_crisis_types) <> 1
               OR array_position(p_crisis_types, NULL) IS NOT NULL
               OR array_position(p_crisis_types, 'underage') IS NOT NULL
               OR array_position(p_crisis_types, p_crisis) IS NULL THEN
                RAISE EXCEPTION 'bad crisis types' USING ERRCODE = '22023';
            END IF;
            v_types := ARRAY(SELECT DISTINCT e FROM unnest(p_crisis_types) AS e ORDER BY e);
        END IF;@LOCK_LEASED@
        -- Кризис сильнее распада пары (О-3): причина safety и тип — для помощи автору (§9.3).
        UPDATE public.partner_messages
           SET status = 'withheld', withheld_reason = p_reason, withheld_crisis = p_crisis,
               withheld_crisis_types = v_types,
               @RELEASE@
         WHERE id = p_message;@EVENTS@
        RETURN true;
    END $f$
""",
    lost="false",
)

WITHHOLD5_SIGNATURE = (
    "pipeline_withhold_leased(uuid, uuid, character varying, safety_event_type[], "
    "safety_event_type)"
)
WITHHOLD6_SIGNATURE = (
    "pipeline_withhold_leased(uuid, uuid, character varying, safety_event_type[], "
    "safety_event_type, safety_event_type[])"
)

FEED_PAGE = R14.FEED_PAGE.replace(
    "withheld_reason varchar, withheld_crisis safety_event_type)",
    "withheld_reason varchar, withheld_crisis safety_event_type,\n"
    "                   withheld_crisis_types safety_event_type[])",
    1,
).replace(
    "               CASE WHEN m.author_id = v_uid THEN m.withheld_crisis END\n",
    "               CASE WHEN m.author_id = v_uid THEN m.withheld_crisis END,\n"
    "               CASE WHEN m.author_id = v_uid THEN m.withheld_crisis_types END\n",
    1,
)
if FEED_PAGE.count("withheld_crisis_types") != 2:
    raise RuntimeError("тело app_couple_feed_page 0014 не совпадает с ожидаемым")
if "withheld_crisis = p_crisis," not in R14.WITHHOLD5:
    raise RuntimeError("тело pipeline_withhold_leased 0014 не совпадает с ожидаемым")


def upgrade() -> None:
    for stmt in SCHEMA:
        op.execute(stmt)
    # Замена, а не перегрузка: у воркера одна функция остановки (страж SECURITY DEFINER — по
    # именам); вызов с 5 аргументами — набор NULL (DEFAULT).
    op.execute(f"DROP FUNCTION {WITHHOLD5_SIGNATURE}")
    op.execute(WITHHOLD6)
    op.execute(f"REVOKE ALL ON FUNCTION {WITHHOLD6_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {WITHHOLD6_SIGNATURE} TO feelings_worker")
    R13._feed(FEED_PAGE)


def downgrade() -> None:
    R13._feed(R14.FEED_PAGE)
    op.execute(f"DROP FUNCTION IF EXISTS {WITHHOLD6_SIGNATURE}")
    op.execute(R14.WITHHOLD5)
    op.execute(f"REVOKE ALL ON FUNCTION {WITHHOLD5_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {WITHHOLD5_SIGNATURE} TO feelings_worker")
    for stmt in DROP_SCHEMA:
        op.execute(stmt)

"""Страж роли платежей, дни после возврата, статус новой пары; чат (4.4 р.2, 2.2 р.2, A1).

Чат: имена CHECK 0018, отметка «события безопасности записаны» у реплики владельца.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-26 23:50:00

ФТ-05, ФТ-17, ФТ-19; ТЗ §7, §9.2, §9.3, §11 п.1, п.6; DECISIONS.md АР-50 (пп. 4, 7), АР-51.
Ревью 4.4 э.1 р.2 (A6) MAJOR-1, MAJOR-2, MINOR-1, MINOR-3; тест р.2 (A8) A8-4.4-D3; ревью 2.2 р.2
MINOR-N1; тест 2.2 р.2 A8-2.2-D5.

**Роль платежей (MAJOR-1, A8-4.4-D3).** 0019 проверял у существующей ``feelings_billing`` только
атрибуты. Теперь страж ``migrations/billing_guard.py`` останавливает миграцию,
если роль — член любой роли, если в неё входят (в любом виде, с вложенностью) роли приложения
``feelings_app``/``feelings_api``/``feelings_worker``/``feelings_admin``, или если стать ею
(``SET``) или получить её права (``USAGE``) может кто-то, кроме суперпользователей и роли входа бота
``feelings_billing_login`` (имя фиксировано, BACKLOG Б-97), а также если роль входа бота входит в
другие роли. Тот же страж исполняет ``migrations/env.py`` при **каждом** запуске Alembic — роль,
испорченная после выкладки, остановит следующий ``alembic upgrade``. Здесь он исполняется ещё раз
внутри ревизии (и попадает в SQL офлайн-режима). С ревью 4.4 р.3 (N1, A8-4.4-D4, R5, R6) страж
строже: ADMIN-членство в роли платежей и в роли входа бота — только у владельца схемы и роли,
исполняющей миграции; у роли входа бота — без ADMIN OPTION и особых атрибутов; у роли платежей —
никаких прав, кроме ``EXECUTE`` на функции бота, ``USAGE`` на ``public`` и ``CONNECT``
(``MIGRATION_GUARD_SQL``; ревизия правится на месте — никуда не выкладывалась, АР-50 п.7).

**Проверка тел функций бота (MINOR-3).** Тела собираются подстановкой маркеров ``--…--``; после
сборки проверяется, что маркеров не осталось и в каждой функции бота есть ``billing role only``
(``RuntimeError``); после ``CREATE`` то же проверяется в БД по ``pg_proc.prosrc`` всех четырёх
``app_bot_payment_*`` (в том числе не изменённых здесь) и маркеров нет ни в одной функции схемы.

**Оплаченные дни после возврата (MAJOR-2, АР-50 п.4).** В 0019 новый период начинался в конце
непрерывного доступа от ``now()`` и длился ровно ``period_days``: если в разрыве после возврата
следующий оплаченный период начинался раньше, чем кончался новый, перекрытие пропадало (пример
ревью: P1 [-10 д, +20 д] возвращён, P2 [+20, +50], оплата 30 дней — доступ до +50, 10 дней
потеряно). Теперь начало прежнее, а конец — «естественный» ``начало + period_days``, продлённый
на длину всех будущих (``trial``/``active``) периодов, которые начинаются внутри нового: период
длится, пока не наберётся ``period_days`` времени, не покрытого другими периодами (в примере —
P3 [now, +60]: 20 дней до P2, 30 дней P2, ещё 10 после). Одна строка ``subscriptions`` на платёж
(``payments.subscription_id`` — одна ссылка); следующие периоды не сдвигаются. Длина перекрытия
добавляется в секундах (без календарных суток — без сдвига на час при переходе на летнее время).
Если позже вернут перекрытый период (P2), его время у P3 остаётся (пара получает больше, а не
меньше оплаченного) — допущение, АР-50 п.4. Без перекрытия — как в 0019.

**Статус (MINOR-1).** Ветка «после распада» (статус дослуживаемого периода распавшейся пары)
берётся, только если у пользователя нет **активной** пары: при новой активной паре без
покрывающего периода статус — её собственный (``expired``/``canceled``/``none``), ``has_access``
по-прежнему учитывает дослуживание (§11 п.6), ``paid_by_me`` — по текущей паре. При ожидающей
паре и без пары — как в 0019.

**Чат (2.2).**

- Имена CHECK 0018 (A8-2.2-D5): соглашение ``ck_%(table_name)s_%(constraint_name)s`` удвоило
  префикс (``ck_conversation_messages_ck_conversation_messages_owner_b5e5`` / ``…_agent_afa2``).
  Ограничения получают имена из модели и описания 0018: ``ck_conversation_messages_owner_only_
  fields`` / ``ck_conversation_messages_agent_only_fields``.
- ``safety_recorded`` (boolean, NOT NULL, false; ревью 2.2 р.2 MINOR-N1) — у реплики владельца:
  события безопасности (``safety_events``) по ней уже записаны. Запись событий и отметка — одна
  транзакция (``repositories.chat.record_safety_events``): повтор с тем же ключом после кризиса,
  ответ на который не сохранился, второго события на ту же реплику не пишет (ФТ-17, АР-51).
  Отступление от §7 — АР-51. Только у ``author = 'owner'`` — добавлено в ``owner_only_fields``.
  Текстов нет; RLS 0001 ``conv_messages_owner`` покрывает колонку, прав не добавляется.

**Downgrade** — тела ``app_subscription_status``/``app_bot_payment_credit`` из 0019, имена CHECK
как в 0018 (их снимает downgrade 0018), колонка ``safety_recorded`` удаляется (отметки теряются:
повтор кризисной реплики после отката может записать событие ещё раз). Права не меняются.
Периоды, начатые по правилу 0020, остаются с теми датами. RoadMap §4 / АР-4: 0020 — только на
локальную и временные тестовые БД до подтверждения оператора (как 0017–0019); откат на стенде с
платежами — только при выключенном приёме платежей.
"""

import importlib.util
import re
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

T = "conversation_messages"
# Имена CHECK, которые дал 0018 (соглашение имён удвоило префикс и укоротило до 63 символов).
OWNER_CK_0018 = "ck_conversation_messages_ck_conversation_messages_owner_b5e5"
AGENT_CK_0018 = "ck_conversation_messages_ck_conversation_messages_agent_afa2"
OWNER_CK = "ck_conversation_messages_owner_only_fields"
AGENT_CK = "ck_conversation_messages_agent_only_fields"
OWNER_ONLY_0018 = (
    "author = 'owner' OR (client_message_id IS NULL AND reply_lease_until IS NULL "
    "AND NOT limit_charged)"
)
OWNER_ONLY = (
    "author = 'owner' OR (client_message_id IS NULL AND reply_lease_until IS NULL "
    "AND NOT limit_charged AND NOT safety_recorded)"
)

MARKER = re.compile(r"--[A-Z_]+--")
BILLING_CHECK = "billing role only"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"не найден модуль: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rev(name: str) -> ModuleType:
    path = Path(__file__).with_name(name)
    return _load(path, f"_rev_{path.stem}")


def guard_sql() -> str:
    path = Path(__file__).resolve().parent.parent / "billing_guard.py"
    return str(_load(path, "_feelings_billing_guard_0020").MIGRATION_GUARD_SQL)


def _sub(sql: str, old: str, new: str) -> str:
    """Замена ровно одного вхождения — правка тел 0017/0019 не пройдёт мимо молча."""
    if sql.count(old) != 1:
        raise RuntimeError(f"ожидалось одно вхождение: {old!r}")
    return sql.replace(old, new)


def checked(sql: str, *, bot: bool) -> str:
    """Тело после подстановки (ревью 4.4 р.2 MINOR-3): маркеров ``--…--`` не осталось; у функции
    бота есть внутренняя проверка роли платежей."""
    left = MARKER.findall(sql)
    if left:
        raise RuntimeError(f"не подставлены маркеры: {sorted(set(left))}")
    if bot and BILLING_CHECK not in sql:
        raise RuntimeError("в функции бота нет проверки роли платежей")
    return sql


def expand(sql: str, markers: dict[str, str], *, bot: bool) -> str:
    for marker, body in markers.items():
        sql = sql.replace(marker, body)
    return checked(sql, bot=bot)


def _markers() -> dict[str, str]:
    r17, r19 = _rev("0017_subscription_payments.py"), _rev("0019_billing_role.py")
    return {
        "--SERVICE_ONLY--": r19._BILLING_ONLY,
        "--USER_KNOWN--": r17._USER_KNOWN,
        "--USER_KEY_SHARE--": r17._USER_KEY_SHARE,
    }


# Статус: «после распада» — только без активной пары (MINOR-1).
_OLD_DISSOLVED = "        IF v_until IS NULL AND has_access THEN\n"
_NEW_DISSOLVED = (
    "        -- Активная новая пара без покрывающего периода — её собственный статус (0020).\n"
    "        IF v_until IS NULL AND has_access AND v_cstatus IS DISTINCT FROM 'active' THEN\n"
)

# Зачисление: конец периода продлевается на будущие периоды внутри него (MAJOR-2).
_OLD_DECLARE = "        v_start timestamptz; v_sub uuid; v_end timestamptz; v_pay uuid;\n"
_NEW_DECLARE = (
    "        v_start timestamptz; v_sub uuid; v_end timestamptz; v_pay uuid;\n"
    "        v_finish timestamptz; v_t timestamptz; v_next timestamptz;\n"
)
_NEW_FINISH = """
        -- Оплаченные дни не теряются (0020, ревью 4.4 р.2 MAJOR-2): «естественный» конец
        -- продлевается на длину будущих периодов, начавшихся внутри нового (в секундах).
        v_finish := v_start + make_interval(days => inv.period_days);
        v_t := v_start;
        LOOP
            SELECT min(s.period_start) INTO v_next FROM public.subscriptions s
             WHERE s.couple_id = inv.couple_id AND s.status IN ('trial', 'active')
               AND s.period_start > v_t;
            EXIT WHEN v_next IS NULL OR v_next >= v_finish;
            v_t := coalesce(public.subscription_covered_until(inv.couple_id, v_next), v_next);
            v_finish := v_finish
                + make_interval(secs => extract(epoch FROM v_t) - extract(epoch FROM v_next));
        END LOOP;
"""
_OLD_VALUES = (
    "        VALUES (inv.couple_id, v_payer, inv.plan, 'active', v_start,\n"
    "                v_start + make_interval(days => inv.period_days))\n"
)
_NEW_VALUES = "        VALUES (inv.couple_id, v_payer, inv.plan, 'active', v_start, v_finish)\n"

# В БД: у каждой из четырёх функций бота — проверка роли; маркеров нет ни в одной функции схемы.
CHECK_BODIES_SQL = r"""
DO $chk$
DECLARE v_bad text; v_n int;
BEGIN
    SELECT count(*) INTO v_n FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.proname LIKE 'app\_bot\_payment\_%';
    IF v_n <> 4 THEN
        RAISE EXCEPTION 'ожидались четыре функции app_bot_payment_*, найдено %', v_n;
    END IF;
    SELECT string_agg(p.proname, ', ' ORDER BY p.proname) INTO v_bad
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.proname LIKE 'app\_bot\_payment\_%'
       AND position('billing role only' IN p.prosrc) = 0;
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'функции бота без проверки роли платежей: %', v_bad;
    END IF;
    SELECT string_agg(p.proname, ', ' ORDER BY p.proname) INTO v_bad
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.prosrc ~ '--[A-Z_]+--';
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'не подставлены маркеры в функциях: %', v_bad;
    END IF;
END $chk$
"""


def _sql_0019() -> tuple[str, str]:
    """Тела ``app_subscription_status`` и ``app_bot_payment_credit`` так, как их создаёт 0019."""
    r19 = _rev("0019_billing_role.py")
    sql = r19._upgrade_sql()
    status, credit = sql[0], sql[3]
    if "FUNCTION app_subscription_status()" not in status or (
        "FUNCTION app_bot_payment_credit(" not in credit
    ):
        raise RuntimeError("порядок тел 0019 изменился")
    return checked(status, bot=False), checked(credit, bot=True)


def upgrade_sql() -> list[str]:
    r17, r19 = _rev("0017_subscription_payments.py"), _rev("0019_billing_role.py")
    markers = _markers()
    status = _sub(r19.STATUS, _OLD_DISSOLVED, _NEW_DISSOLVED)
    credit = r19._sub(r17.CREDIT, r19._OLD_START, r19._NEW_START + _NEW_FINISH)
    credit = _sub(_sub(credit, _OLD_DECLARE, _NEW_DECLARE), _OLD_VALUES, _NEW_VALUES)
    return [
        expand(status, markers, bot=False),
        expand(r19._replace(credit), markers, bot=True),
    ]


def upgrade() -> None:
    op.execute(guard_sql())
    for stmt in upgrade_sql():
        op.execute(stmt)
    op.execute(CHECK_BODIES_SQL)
    op.execute(f"ALTER TABLE {T} RENAME CONSTRAINT {AGENT_CK_0018} TO {AGENT_CK}")
    op.drop_constraint(op.f(OWNER_CK_0018), T, type_="check")
    op.add_column(
        T,
        sa.Column("safety_recorded", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_check_constraint(op.f(OWNER_CK), T, OWNER_ONLY)


def downgrade() -> None:
    op.drop_constraint(op.f(OWNER_CK), T, type_="check")
    op.drop_column(T, "safety_recorded")
    op.create_check_constraint(op.f(OWNER_CK_0018), T, OWNER_ONLY_0018)
    op.execute(f"ALTER TABLE {T} RENAME CONSTRAINT {AGENT_CK} TO {AGENT_CK_0018}")
    for stmt in _sql_0019():
        op.execute(stmt)

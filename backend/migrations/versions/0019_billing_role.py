"""Роль платежей ``feelings_billing``, доступ после возврата, статус после распада (4.4 э.1 р.1, A1).

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-26 23:00:00

ФТ-19; ТЗ §7, §9.2, §11 п.1, п.6; DECISIONS.md АР-10, АР-13, АР-38, АР-50 (пп. 4, 7).
Ревью 4.4 э.1 р.1 (A6) MINOR-2, 3, 4, 5, 9; тест р.1 (A8) D1, D2.

**Роль платежей (АР-50 п.7; ревью MINOR-2, A8-4.4-D2).** В 0017 функции бота
``app_bot_payment_*`` исполняла ``feelings_app``, а служебный контекст отличался от сессии
пользователя только пустым ``app.user_id``: код в сессии пользователя после ``RESET app.user_id``
мог зачислить себе период. Теперь:

- групповая роль ``feelings_billing`` — ``NOLOGIN NOINHERIT``, без особых атрибутов, без прав на
  таблицы и без членств; создаётся, если её нет (роли общие для кластера, как в 0001); уже
  существующая с ``LOGIN``/``INHERIT``/``SUPERUSER``/``CREATEROLE``/``CREATEDB``/``REPLICATION``/
  ``BYPASSRLS`` — миграция останавливается, ничего не меняя;
- ``USAGE`` на схему ``public`` и ``EXECUTE`` на четыре функции ``app_bot_payment_*`` — только
  ей; у ``feelings_app`` ``EXECUTE`` на них отозван. Сессия пользователя (``feelings_api`` →
  ``feelings_app``) стать ``feelings_billing`` не может (не член роли), поэтому вызов — 42501
  при любом ``app.user_id``, в том числе после ``RESET``;
- функции сами проверяют контекст: ``current_setting('role') = 'feelings_billing'`` (роль,
  выбранная ``SET ROLE`` вызывающим; SECURITY DEFINER её не меняет) и пустой ``app.user_id``,
  иначе 42501 — на случай ошибочной выдачи ``EXECUTE`` другой роли;
- вход — отдельная роль входа бота по образцу ``infra/db/roles.sql`` (``LOGIN NOINHERIT``, член
  ``feelings_billing`` с ``INHERIT FALSE, SET TRUE``, пароль — из окружения, не из Git), код
  бота делает ``SET LOCAL ROLE feelings_billing`` (как ``admin_scope``). Роль входа, переменную
  окружения бота и проверку в ``roles.sql`` добавляет инфраструктура (A7, BACKLOG Б-97).

**Начало оплаченного периода (АР-50 п.4; A8-4.4-D1, ревью MINOR-4).** Новый период начинается
в ``coalesce(subscription_covered_until(пара, now()), now())`` — в конце **непрерывного**
доступа от ``now()``, а не после последнего будущего периода. Если возврат оставил разрыв
(возвращён текущий период, следующий оплаченный впереди), новая оплата даёт доступ сразу;
следующие периоды по-прежнему не сдвигаются (перекрытие допустимо — доступ считается по
цепочке). Оплата во время действующего периода, как и раньше, встаёт после его конца.

**Статус** ``app_subscription_status()``:

- после распада (ревью MINOR-3): если текущая пара ``now()`` не покрывает (или пары нет), а
  доступ есть — его даёт оплаченный период распавшейся пары (§11 п.6), и ``status = 'active'``,
  ``plan`` и ``period_end`` описывают его (конец непрерывной цепочки той пары), ``paid_by_me`` —
  по той паре; ``couple_id``/``couple_status``/``trial_*`` — по текущей паре (нет — NULL/false);
- без доступа ``status``/``plan``/``period_end`` берутся у последнего **начавшегося** периода
  (будущие периоды разрыва после возврата не показываются как «истёк с будущей датой»).

**Предел счетов (ревью MINOR-5).** ``app_subscription_create_invoice`` берёт пару
``FOR NO KEY UPDATE`` (было ``FOR KEY SHARE``): одновременные вызовы обоих партнёров идут по
очереди, 21-го действующего счёта не бывает; второй одновременный запрос с теми же условиями
получает счёт первого (``reused``). С конвейером (``FOR KEY SHARE`` пары) не конфликтует;
порядок пользователь → пара сохранён (АР-13, АР-38).

**Не меняется.** Таблицы, колонки, CHECK, RLS, функции пользователя, кроме двух названных выше.
Зачисление без счёта и с несовпадением (``invoice_not_found``/``amount_mismatch``) в БД следа
по-прежнему не оставляет: строку ``payments`` без пары и периода запрещает CHECK
``couple_or_subscription``, у ``audit_log`` нет колонки для charge id (а запись с id счёта
противоречила бы «несовпадение — без следов»); правило для бота (немедленный
``refundStarPayment``, журнал приложения — код и charge id) — карточка 4.4, этап 2 (ревью
MINOR-1, A8 R2).

**Downgrade** (ревью MINOR-9; RoadMap §4 — откат на стенде с платежами только по подтверждению
оператора и при **выключенном приёме платежей**: вебхук бота остановлен, новых
``pre_checkout_query``/``successful_payment`` нет): тела функций 0017 возвращаются, ``EXECUTE``
на ``app_bot_payment_*`` — снова ``feelings_app``, у ``feelings_billing`` отзываются ``EXECUTE`` и
``USAGE``. Сама роль не удаляется (общая для кластера, её могут использовать другие базы; как
роли 0001). Данные не меняются: периоды, начатые по правилу 0019, остаются с теми датами.
Пока на стенде нет решения оператора по АР-50 (отступление от §7 в 0017, пп. 1–8), 0017–0019
выкладываются только на локальную и временные тестовые БД (АР-4; ревью MINOR-8).
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BILLING_ROLE = "feelings_billing"


def _rev(name: str) -> ModuleType:
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(f"_rev_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"не найдена ревизия: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sub(sql: str, old: str, new: str) -> str:
    """Замена ровно одного вхождения — чтобы правка тела 0017 не прошла мимо молча."""
    if sql.count(old) != 1:
        raise RuntimeError(f"ожидалось одно вхождение: {old!r}")
    return sql.replace(old, new)


def _replace(sql: str) -> str:
    return _sub(sql, "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION")


# Роль платежей: создать, если нет; существующую с входом или особыми правами — не принимать.
ROLE_SQL = r"""
DO $r$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'feelings_billing') THEN
        CREATE ROLE feelings_billing NOLOGIN NOINHERIT;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'feelings_billing'
               AND (rolcanlogin OR rolinherit OR rolsuper OR rolcreaterole OR rolcreatedb
                    OR rolreplication OR rolbypassrls)) THEN
        RAISE EXCEPTION 'роль feelings_billing уже есть и имеет вход, INHERIT или особые права';
    END IF;
END $r$
"""

# Контекст бота: роль feelings_billing (выбрана SET ROLE) и пустой app.user_id.
_BILLING_ONLY = """
        IF current_setting('role') IS DISTINCT FROM 'feelings_billing' THEN
            RAISE EXCEPTION 'billing role only' USING ERRCODE = '42501';
        END IF;
        IF public.app_uid() IS NOT NULL THEN
            RAISE EXCEPTION 'service context only' USING ERRCODE = '42501';
        END IF;
"""

STATUS = r"""
CREATE OR REPLACE FUNCTION app_subscription_status()
    RETURNS TABLE (couple_id uuid, couple_status couple_status, status text, plan varchar,
                   period_end timestamptz, trial_ends_at timestamptz, trial_used boolean,
                   paid_by_me boolean, has_access boolean)
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE
        v_uid uuid := public.app_uid(); v_couple uuid; v_cstatus public.couple_status;
        v_until timestamptz; v_plan varchar; v_status text; v_end timestamptz;
        v_last record; v_payer uuid; v_paid boolean; v_dc uuid; v_dend timestamptz;
    BEGIN
--USER_KNOWN--
        has_access := public.subscription_user_has_access(v_uid);
        SELECT cm.couple_id, c.status INTO v_couple, v_cstatus
          FROM public.couple_members cm JOIN public.couples c ON c.id = cm.couple_id
         WHERE cm.user_id = v_uid AND cm.active;
        IF v_couple IS NOT NULL THEN
            v_until := public.subscription_covered_until(v_couple, now());
        END IF;
        -- Текущая пара now() не покрывает, а доступ есть: его даёт оплаченный период
        -- распавшейся пары (§11 п.6) — то же условие, что в subscription_user_has_access.
        IF v_until IS NULL AND has_access THEN
            SELECT c.id, cov.until INTO v_dc, v_dend
              FROM public.couple_members cm
              JOIN public.couples c ON c.id = cm.couple_id
             CROSS JOIN LATERAL (SELECT public.subscription_covered_until(c.id, now()) AS until) cov
             WHERE cm.user_id = v_uid AND c.status = 'dissolved' AND cov.until IS NOT NULL
               AND EXISTS (SELECT 1 FROM public.subscriptions s
                            WHERE s.couple_id = c.id AND s.status = 'active'
                              AND s.plan <> 'trial' AND s.period_end > now()
                              AND s.period_start < cov.until)
             ORDER BY cov.until DESC LIMIT 1;
        END IF;
        IF v_dc IS NOT NULL THEN
            SELECT s.plan INTO v_plan FROM public.subscriptions s
             WHERE s.couple_id = v_dc AND s.status = 'active' AND s.plan <> 'trial'
               AND s.period_end > now() AND s.period_start < v_dend
             ORDER BY s.period_end DESC LIMIT 1;
            v_status := 'active'; v_end := v_dend;
        ELSIF v_couple IS NULL THEN
            v_status := 'none'; v_plan := NULL; v_end := NULL;
        ELSIF v_until IS NOT NULL THEN
            SELECT s.plan INTO v_plan FROM public.subscriptions s
             WHERE s.couple_id = v_couple AND s.status IN ('trial', 'active')
               AND s.period_start <= now() AND s.period_end > now()
             ORDER BY (s.plan = 'trial'), s.period_end DESC LIMIT 1;
            v_status := CASE WHEN v_plan = 'trial' THEN 'trial' ELSE 'active' END;
            v_end := v_until;
        ELSE
            -- Последний начавшийся период: будущий период разрыва после возврата — не «истёк».
            SELECT s.status, s.plan, s.period_end INTO v_last FROM public.subscriptions s
             WHERE s.couple_id = v_couple AND s.period_start <= now()
             ORDER BY s.period_start DESC, s.period_end DESC, s.created_at DESC LIMIT 1;
            IF NOT FOUND THEN
                v_status := 'none'; v_plan := NULL; v_end := NULL;
            ELSE
                v_status := CASE WHEN v_last.status = 'canceled' THEN 'canceled' ELSE 'expired' END;
                v_plan := v_last.plan; v_end := v_last.period_end;
            END IF;
        END IF;
        SELECT s.payer_id, true INTO v_payer, v_paid FROM public.subscriptions s
         WHERE s.couple_id = coalesce(v_dc, v_couple) AND s.plan <> 'trial'
           AND s.status = 'active'
         ORDER BY s.period_end DESC, s.created_at DESC LIMIT 1;
        couple_id := v_couple; couple_status := v_cstatus; status := v_status; plan := v_plan;
        period_end := v_end;
        SELECT s.period_end INTO trial_ends_at FROM public.subscriptions s
         WHERE s.couple_id = v_couple AND s.plan = 'trial';
        trial_used := trial_ends_at IS NOT NULL;
        paid_by_me := CASE WHEN v_paid THEN v_payer IS NOT DISTINCT FROM v_uid END;
        RETURN NEXT;
    END $f$
"""

# Начало нового периода — конец непрерывного доступа от now() (A8-4.4-D1).
_OLD_START = """        SELECT greatest(now(), max(s.period_end)) INTO v_start FROM public.subscriptions s
         WHERE s.couple_id = inv.couple_id AND s.status IN ('trial', 'active')
           AND s.period_end > now();
        v_start := coalesce(v_start, now());
"""
_NEW_START = """        -- Конец непрерывного доступа от now(): после возврата с разрывом — сразу (АР-50 п.4).
        v_start := coalesce(public.subscription_covered_until(inv.couple_id, now()), now());
"""

# Предел счетов под блокировкой, исключающей одновременное создание (ревью MINOR-5).
_OLD_INVOICE_LOCK = (
    "SELECT c.status INTO v_cstatus FROM public.couples c WHERE c.id = v_couple FOR KEY SHARE;"
)
_NEW_INVOICE_LOCK = (
    "SELECT c.status INTO v_cstatus FROM public.couples c WHERE c.id = v_couple\n"
    "         FOR NO KEY UPDATE;"
)


def _upgrade_sql() -> list[str]:
    r17 = _rev("0017_subscription_payments.py")
    billing = {
        "--SERVICE_ONLY--": _BILLING_ONLY,
        "--USER_KNOWN--": r17._USER_KNOWN,
        "--USER_KEY_SHARE--": r17._USER_KEY_SHARE,
    }

    def expand(sql: str) -> str:
        for marker, body in billing.items():
            sql = sql.replace(marker, body)
        return sql

    credit = _sub(r17.CREDIT, _OLD_START, _NEW_START)
    invoice = _sub(r17.CREATE_INVOICE, _OLD_INVOICE_LOCK, _NEW_INVOICE_LOCK)
    return [
        expand(STATUS),
        expand(_replace(invoice)),
        expand(_replace(r17.PRECHECK)),
        expand(_replace(credit)),
        expand(_replace(r17.REFUNDED)),
        expand(_replace(r17.LOOKUP)),
    ]


def _downgrade_sql() -> list[str]:
    r17 = _rev("0017_subscription_payments.py")
    return [
        _replace(r17._expand(sql))
        for sql in (
            r17.STATUS,
            r17.CREATE_INVOICE,
            r17.PRECHECK,
            r17.CREDIT,
            r17.REFUNDED,
            r17.LOOKUP,
        )
    ]


def _bot_signatures() -> tuple[str, ...]:
    sigs = tuple(s for s in _rev("0017_subscription_payments.py").APP_SIGNATURES if "_bot_" in s)
    if len(sigs) != 4:
        raise RuntimeError("ожидались четыре функции app_bot_payment_*")
    return sigs


def upgrade() -> None:
    op.execute(ROLE_SQL)
    op.execute(f"GRANT USAGE ON SCHEMA public TO {BILLING_ROLE}")
    # CREATE OR REPLACE сохраняет права функций; ниже они перевыдаются явно.
    for stmt in _upgrade_sql():
        op.execute(stmt)
    for sig in _bot_signatures():
        op.execute(f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC, feelings_app")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO {BILLING_ROLE}")


def downgrade() -> None:
    for stmt in _downgrade_sql():
        op.execute(stmt)
    for sig in _bot_signatures():
        op.execute(f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC, {BILLING_ROLE}")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO feelings_app")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {BILLING_ROLE}")

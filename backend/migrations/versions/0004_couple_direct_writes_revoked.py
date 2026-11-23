"""Пара: изменения только через функции; выдача ссылки — после онбординга (задача 1.3, A1).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-21 22:00:00

ФТ-03, ФТ-04; ТЗ §4.1 п.2–6, §4.5 п.2, §7, §9.3; DECISIONS.md АР-7, АР-12, АР-13.

1. BACKLOG Б-13. У ``feelings_app`` отзываются прямые права записи, оставшиеся от 0001:
   ``UPDATE (status, dissolved_at) ON couples``, ``UPDATE (active, left_at) ON couple_members``,
   ``INSERT`` и ``UPDATE (status) ON invites``. Прямой ``UPDATE`` позволял выйти, не распустив
   пару для второго участника и не отозвав ссылки (ТЗ §4.5 п.2, ФТ-04). Теперь пара, участники
   и приглашения меняются только функциями SECURITY DEFINER: ``app_create_couple``,
   ``app_issue_invite``, ``app_accept_invite``, ``app_leave_couple``. ``SELECT`` и политики
   чтения (``couples_select``, ``members_select``, ``invites_select``) остаются.

   Политики записи ``couples_dissolve``, ``members_leave``, ``invites_insert``,
   ``invites_revoke`` удаляются, а не оставляются «безвредными»: без прав на таблицу они не
   действуют, но если право когда-нибудь вернут по ошибке (GRANT в новой миграции), при
   включённом RLS и без разрешающей политики запись всё равно будет отклонена, а не пропущена
   по устаревшим условиям 0001. Функции SECURITY DEFINER выполняются от владельца таблиц,
   на которого RLS не действует (FORCE ROW LEVEL SECURITY не включён), — политики им не нужны.

2. Тесты 0.7b, D-4. ``app_issue_invite`` требует завершённого онбординга (все согласия и 18+,
   ТЗ §4.1 п.2, п.4, §9.3), как ``app_create_couple`` и ``app_accept_invite``: иначе ``FB008``.
   Порядок проверок и блокировок прежний: ``22023`` (хеш) → строка пользователя
   (``42501``, если нет или удалён) → ``FB008`` → членство (``FB007``) → приглашения → пара
   (``FB006``). Выход из пары (``app_leave_couple``) онбординга по-прежнему не требует.

Downgrade возвращает права и политики 0001 и определение ``app_issue_invite`` из 0003.
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rev0003() -> ModuleType:
    """Ревизия 0003 — общие фрагменты функций и определение для downgrade."""
    path = Path(__file__).with_name("0003_couple_functions_fixes.py")
    spec = importlib.util.spec_from_file_location("_rev0003", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"не найдена ревизия 0003: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Как в 0003, плюс --REQUIRE_ONBOARDED-- сразу после блокировки строки пользователя.
ISSUE_INVITE = r"""
CREATE OR REPLACE FUNCTION app_issue_invite(p_token_hash bytea) RETURNS timestamptz
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE
        v_uid uuid := public.app_uid(); v_onboarded boolean;
        v_couple uuid; v_status public.couple_status; v_expires timestamptz;
    BEGIN
        IF p_token_hash IS NULL OR octet_length(p_token_hash) <> 32 THEN
            RAISE EXCEPTION 'bad token hash' USING ERRCODE = '22023';
        END IF;
--LOCK_USER--
--REQUIRE_ONBOARDED--
        SELECT cm.couple_id INTO v_couple FROM public.couple_members cm
            WHERE cm.user_id = v_uid AND cm.active;
        IF v_couple IS NULL THEN
            RAISE EXCEPTION 'no active membership' USING ERRCODE = 'FB007';
        END IF;
        PERFORM 1 FROM public.invites
            WHERE couple_id = v_couple AND status = 'active' FOR UPDATE;
        SELECT c.status INTO v_status FROM public.couples c WHERE c.id = v_couple FOR UPDATE;
        IF v_status <> 'pending' THEN
            RAISE EXCEPTION 'couple not pending' USING ERRCODE = 'FB006';
        END IF;
        UPDATE public.invites SET status = 'revoked'
            WHERE couple_id = v_couple AND status = 'active';
        INSERT INTO public.invites (token_hash, couple_id, created_by, expires_at)
        VALUES (p_token_hash, v_couple, v_uid, now() + interval '7 days')
        RETURNING expires_at INTO v_expires;
        RETURN v_expires;
    END $f$
"""

REVOKE_DIRECT_WRITES = (
    "REVOKE UPDATE (status, dissolved_at) ON couples FROM feelings_app",
    "REVOKE UPDATE (active, left_at) ON couple_members FROM feelings_app",
    "REVOKE INSERT ON invites FROM feelings_app",
    "REVOKE UPDATE (status) ON invites FROM feelings_app",
    "DROP POLICY IF EXISTS couples_dissolve ON couples",
    "DROP POLICY IF EXISTS members_leave ON couple_members",
    "DROP POLICY IF EXISTS invites_insert ON invites",
    "DROP POLICY IF EXISTS invites_revoke ON invites",
)

# Права и политики 0001 — для downgrade (тексты политик совпадают с 0001).
RESTORE_DIRECT_WRITES = (
    "GRANT UPDATE (status, dissolved_at) ON couples TO feelings_app",
    "GRANT UPDATE (active, left_at) ON couple_members TO feelings_app",
    "GRANT INSERT ON invites TO feelings_app",
    "GRANT UPDATE (status) ON invites TO feelings_app",
    """CREATE POLICY couples_dissolve ON couples FOR UPDATE TO feelings_app
    USING (app_is_active_member(id)) WITH CHECK (status = 'dissolved')""",
    """CREATE POLICY members_leave ON couple_members FOR UPDATE TO feelings_app
    USING (user_id = app_uid() AND active) WITH CHECK (user_id = app_uid() AND NOT active)""",
    """CREATE POLICY invites_insert ON invites FOR INSERT TO feelings_app
    WITH CHECK (created_by = app_uid() AND status = 'active' AND used_by IS NULL
                AND used_at IS NULL AND expires_at <= now() + interval '7 days'
                AND app_is_active_member(couple_id)
                AND EXISTS (SELECT 1 FROM couples c WHERE c.id = couple_id
                            AND c.status = 'pending'))""",
    """CREATE POLICY invites_revoke ON invites FOR UPDATE TO feelings_app
    USING (created_by = app_uid() AND status = 'active') WITH CHECK (status = 'revoked')""",
)


def upgrade() -> None:
    # По одной команде: asyncpg не выполняет несколько команд в одном запросе.
    op.execute(_rev0003()._expand(ISSUE_INVITE))
    for stmt in REVOKE_DIRECT_WRITES:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in RESTORE_DIRECT_WRITES:
        op.execute(stmt)
    old = _rev0003()
    op.execute(old._expand(old.ISSUE_INVITE))

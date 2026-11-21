"""Пара: исправления функций 0002 по ревью 0.7b, раунд 1 (задача 0.7b, A1).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-21 20:00:00

Заменяет (CREATE OR REPLACE) ``app_create_couple`` (0001), ``app_issue_invite``,
``app_accept_invite``, ``app_leave_couple`` (0002). Сигнатуры и права не меняются.

- Все четыре функции блокируют строку своего пользователя первой (порядок пользователь →
  приглашения → пара, ревью n7) и требуют, чтобы он существовал и не был удалён: иначе
  ``42501`` (тесты A8, D-3; раньше — ошибка FK 23503).
- ``app_create_couple`` и ``app_accept_invite`` — только после онбординга: все согласия и
  подтверждение 18+ (ТЗ §4.1 п.2, п.4, §9.3), иначе ``FB008`` (ревью n1).
- ``app_accept_invite`` проверяет хеш токена так же, как ``app_issue_invite``: NULL или не
  32 байта — ``22023`` (ревью n5, A8 D-2).
- ``app_leave_couple`` перепроверяет членство после блокировки пары: второй одновременный
  выход получает ``FB007`` и не перезаписывает ``dissolved_at`` (ревью n3); пишет в
  ``audit_log`` действие ``couple.leave`` (actor_id — вышедший, object_type ``couple``,
  object_id — пара), без текстов (ревью n2).

Коды ошибок — DECISIONS.md, АР-13 (FB001…FB008). ``app_create_couple`` при уже активном
членстве по-прежнему отвечает ``23505`` уникального индекса
``uq_couple_members_one_active_per_user``; блокировка строки пользователя делает так, что
одновременное с ним принятие приглашения тем же пользователем получает ``FB005``.

Downgrade возвращает определения 0001/0002 без изменений.
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Общее начало: пользователь известен, существует, не удалён; его строка заблокирована.
_LOCK_USER = """
        IF v_uid IS NULL THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        SELECT u.consent_terms_at IS NOT NULL AND u.consent_data_at IS NOT NULL
               AND u.consent_sensitive_at IS NOT NULL AND u.age_confirmed_at IS NOT NULL
          INTO v_onboarded
          FROM public.users u WHERE u.id = v_uid AND u.deleted_at IS NULL FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
"""

_REQUIRE_ONBOARDED = """
        IF NOT v_onboarded THEN
            RAISE EXCEPTION 'onboarding not completed' USING ERRCODE = 'FB008';
        END IF;
"""

CREATE_COUPLE = r"""
CREATE OR REPLACE FUNCTION app_create_couple() RETURNS uuid
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE v_uid uuid := public.app_uid(); v_onboarded boolean; v_id uuid;
    BEGIN
--LOCK_USER--
--REQUIRE_ONBOARDED--
        INSERT INTO public.couples (status) VALUES ('pending') RETURNING id INTO v_id;
        INSERT INTO public.couple_members (couple_id, user_id, role)
        VALUES (v_id, v_uid, 'initiator');
        RETURN v_id;
    END $f$
"""

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

ACCEPT_INVITE = r"""
CREATE OR REPLACE FUNCTION app_accept_invite(p_token_hash bytea) RETURNS uuid
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE
        v_uid uuid := public.app_uid(); v_onboarded boolean;
        i record; v_status public.couple_status; v_active integer;
    BEGIN
        IF p_token_hash IS NULL OR octet_length(p_token_hash) <> 32 THEN
            RAISE EXCEPTION 'bad token hash' USING ERRCODE = '22023';
        END IF;
--LOCK_USER--
--REQUIRE_ONBOARDED--
        -- Блокировка строки приглашения: второе одновременное принятие ждёт и видит used.
        SELECT * INTO i FROM public.invites WHERE token_hash = p_token_hash FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'invite not found' USING ERRCODE = 'FB001';
        END IF;
        IF i.created_by = v_uid THEN
            RAISE EXCEPTION 'own invite' USING ERRCODE = 'FB004';
        END IF;
        IF i.status IN ('used', 'revoked') THEN
            RAISE EXCEPTION 'invite used or revoked' USING ERRCODE = 'FB003';
        END IF;
        IF i.status = 'expired' OR i.expires_at <= now() THEN
            RAISE EXCEPTION 'invite expired' USING ERRCODE = 'FB002';
        END IF;
        IF EXISTS (SELECT 1 FROM public.couple_members
                    WHERE user_id = v_uid AND active) THEN
            RAISE EXCEPTION 'already in couple' USING ERRCODE = 'FB005';
        END IF;
        SELECT c.status INTO v_status FROM public.couples c WHERE c.id = i.couple_id FOR UPDATE;
        SELECT count(*) INTO v_active FROM public.couple_members
            WHERE couple_id = i.couple_id AND active;
        IF v_status IS DISTINCT FROM 'pending' OR v_active <> 1 THEN
            RAISE EXCEPTION 'couple not pending' USING ERRCODE = 'FB006';
        END IF;
        UPDATE public.invites SET status = 'used', used_by = v_uid, used_at = now()
            WHERE id = i.id;
        INSERT INTO public.couple_members (couple_id, user_id, role)
        VALUES (i.couple_id, v_uid, 'partner');
        UPDATE public.couples SET status = 'active', activated_at = now()
            WHERE id = i.couple_id;
        RETURN i.couple_id;
    END $f$
"""

LEAVE_COUPLE = r"""
CREATE OR REPLACE FUNCTION app_leave_couple() RETURNS uuid
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE v_uid uuid := public.app_uid(); v_onboarded boolean; v_couple uuid;
    BEGIN
--LOCK_USER--
        SELECT cm.couple_id INTO v_couple FROM public.couple_members cm
            WHERE cm.user_id = v_uid AND cm.active;
        IF v_couple IS NULL THEN
            RAISE EXCEPTION 'no active membership' USING ERRCODE = 'FB007';
        END IF;
        PERFORM 1 FROM public.invites
            WHERE couple_id = v_couple AND status = 'active' FOR UPDATE;
        PERFORM 1 FROM public.couples WHERE id = v_couple FOR UPDATE;
        -- Перепроверка после блокировки пары: партнёр мог выйти раньше (ревью n3).
        IF NOT EXISTS (SELECT 1 FROM public.couple_members
                        WHERE couple_id = v_couple AND user_id = v_uid AND active) THEN
            RAISE EXCEPTION 'no active membership' USING ERRCODE = 'FB007';
        END IF;
        -- Пара из двух: с выходом одного распадается для обоих (ТЗ §4.5 п.2, АР-13).
        UPDATE public.couple_members SET active = false, left_at = now()
            WHERE couple_id = v_couple AND active;
        UPDATE public.couples SET status = 'dissolved', dissolved_at = now()
            WHERE id = v_couple AND status <> 'dissolved';
        UPDATE public.invites SET status = 'revoked'
            WHERE couple_id = v_couple AND status = 'active';
        -- Кто вышел (ТЗ §7 audit_log: кто, действие, объект, время), без текстов.
        INSERT INTO public.audit_log (actor_id, action, object_type, object_id)
        VALUES (v_uid, 'couple.leave', 'couple', v_couple);
        RETURN v_couple;
    END $f$
"""

# Определение из 0001 — для downgrade.
CREATE_COUPLE_0001 = r"""
CREATE OR REPLACE FUNCTION app_create_couple() RETURNS uuid
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE v_uid uuid := public.app_uid(); v_id uuid;
    BEGIN
        IF v_uid IS NULL THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        INSERT INTO public.couples (status) VALUES ('pending') RETURNING id INTO v_id;
        INSERT INTO public.couple_members (couple_id, user_id, role)
        VALUES (v_id, v_uid, 'initiator');
        RETURN v_id;
    END $f$
"""


def _rev0002() -> ModuleType:
    """Определения 0002 — источник для downgrade, чтобы не дублировать тексты функций."""
    path = Path(__file__).with_name("0002_couple_functions.py")
    spec = importlib.util.spec_from_file_location("_rev0002", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"не найдена ревизия 0002: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _expand(sql: str) -> str:
    """Подставляет общие фрагменты на место меток ``--LOCK_USER--`` и ``--REQUIRE_ONBOARDED--``."""
    return sql.replace("--LOCK_USER--", _LOCK_USER).replace(
        "--REQUIRE_ONBOARDED--", _REQUIRE_ONBOARDED
    )


def _or_replace(sql: str) -> str:
    return sql.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


def upgrade() -> None:
    # По одной команде: asyncpg не выполняет несколько команд в одном запросе.
    for stmt in (CREATE_COUPLE, ISSUE_INVITE, ACCEPT_INVITE, LEAVE_COUPLE):
        op.execute(_expand(stmt))


def downgrade() -> None:
    old = _rev0002()
    for stmt in (
        CREATE_COUPLE_0001,
        _or_replace(old.ISSUE_INVITE),
        _or_replace(old.ACCEPT_INVITE),
        _or_replace(old.LEAVE_COUPLE),
    ):
        op.execute(stmt)

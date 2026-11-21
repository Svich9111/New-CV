"""Пара: выдача и принятие приглашения, выход из пары (задача 0.7b, A1).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-21 18:00:00

ФТ-03, ФТ-04; ТЗ §4.1 п.3–6, §4.5 п.2, §7. Вступление в пару — только функцией
SECURITY DEFINER (АР-7, АР-12). Функции выполняет только роль ``feelings_app``.

Функции:

- ``app_issue_invite(p_token_hash bytea) RETURNS timestamptz`` — новая ссылка для своей
  ожидающей пары: прежние ``active``-приглашения пары отзываются (``revoked``), срок —
  7 дней от ``now()`` ставит функция (ТЗ §4.1 п.6). Возвращает ``expires_at``.
- ``app_accept_invite(p_token_hash bytea) RETURNS uuid`` — принять приглашение: оно
  становится ``used`` (``used_by``, ``used_at``), принимающий — участник ``partner``, пара —
  ``active`` (``activated_at``). Возвращает ``couple_id``.
- ``app_leave_couple() RETURNS uuid`` — выйти из своей пары (активной или ожидающей):
  все активные участники пары — ``active = false``, ``left_at``; пара — ``dissolved``
  (``dissolved_at``); ``active``-приглашения пары — ``revoked``. Агенты и память не
  затрагиваются (ТЗ §4.5 п.2); после выхода оба могут создать новую пару (ФТ-04).
  Возвращает ``couple_id``.

Коды ошибок (SQLSTATE, класс FB; DECISIONS.md, АР-13):

- ``FB001`` — приглашение не найдено;
- ``FB002`` — приглашение истекло (``expires_at <= now()`` или статус ``expired``);
- ``FB003`` — приглашение уже использовано или отозвано;
- ``FB004`` — своё приглашение (создатель не может его принять);
- ``FB005`` — у принимающего уже есть активное членство в паре (одна пара, ФТ-04);
- ``FB006`` — пара не ожидает партнёра (не ``pending`` или в ней не один активный участник);
- ``FB007`` — у текущего пользователя нет активного членства в паре (выход, выдача ссылки).

Прочие: ``42501`` — нет ``app.user_id``; ``22023`` — хеш токена не 32 байта (SHA-256).

Гонки: строки блокируются в одном порядке — пользователь → приглашения → пара, поэтому
два одновременных принятия одного приглашения дают ровно один успех (второе после
ожидания видит ``used`` → ``FB003``), а принятие и выход/перевыпуск не взаимоблокируются.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ISSUE_INVITE = r"""
CREATE FUNCTION app_issue_invite(p_token_hash bytea) RETURNS timestamptz
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE
        v_uid uuid := public.app_uid();
        v_couple uuid; v_status public.couple_status; v_expires timestamptz;
    BEGIN
        IF v_uid IS NULL THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        IF p_token_hash IS NULL OR octet_length(p_token_hash) <> 32 THEN
            RAISE EXCEPTION 'bad token hash' USING ERRCODE = '22023';
        END IF;
        PERFORM 1 FROM public.users WHERE id = v_uid FOR UPDATE;
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
CREATE FUNCTION app_accept_invite(p_token_hash bytea) RETURNS uuid
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE
        v_uid uuid := public.app_uid();
        i record; v_status public.couple_status; v_active integer;
    BEGIN
        IF v_uid IS NULL THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        PERFORM 1 FROM public.users WHERE id = v_uid FOR UPDATE;
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
CREATE FUNCTION app_leave_couple() RETURNS uuid
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE v_uid uuid := public.app_uid(); v_couple uuid;
    BEGIN
        IF v_uid IS NULL THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        PERFORM 1 FROM public.users WHERE id = v_uid FOR UPDATE;
        SELECT cm.couple_id INTO v_couple FROM public.couple_members cm
            WHERE cm.user_id = v_uid AND cm.active;
        IF v_couple IS NULL THEN
            RAISE EXCEPTION 'no active membership' USING ERRCODE = 'FB007';
        END IF;
        PERFORM 1 FROM public.invites
            WHERE couple_id = v_couple AND status = 'active' FOR UPDATE;
        PERFORM 1 FROM public.couples WHERE id = v_couple FOR UPDATE;
        -- Пара из двух: с выходом одного распадается для обоих (ТЗ §4.5 п.2), второй
        -- участник освобождается и может создать новую пару (ФТ-04).
        UPDATE public.couple_members SET active = false, left_at = now()
            WHERE couple_id = v_couple AND active;
        UPDATE public.couples SET status = 'dissolved', dissolved_at = now()
            WHERE id = v_couple;
        UPDATE public.invites SET status = 'revoked'
            WHERE couple_id = v_couple AND status = 'active';
        RETURN v_couple;
    END $f$
"""

UPGRADE = (
    ISSUE_INVITE,
    ACCEPT_INVITE,
    LEAVE_COUPLE,
    "REVOKE ALL ON FUNCTION app_issue_invite(bytea), app_accept_invite(bytea), "
    "app_leave_couple() FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION app_issue_invite(bytea), app_accept_invite(bytea), "
    "app_leave_couple() TO feelings_app",
)

DOWNGRADE = (
    "DROP FUNCTION IF EXISTS app_leave_couple()",
    "DROP FUNCTION IF EXISTS app_accept_invite(bytea)",
    "DROP FUNCTION IF EXISTS app_issue_invite(bytea)",
)


def upgrade() -> None:
    # По одной команде: asyncpg не выполняет несколько команд в одном запросе.
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)

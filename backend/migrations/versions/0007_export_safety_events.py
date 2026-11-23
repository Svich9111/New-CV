"""Выгрузка своих срабатываний кризисного протокола: app_export_safety_events() (задача 4.5, этап 2, A1).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-22 00:30:00

ФТ-21; ТЗ §7 («safety_events: id, user_id, тип, время. Без текста переписки»), §9.1 п.6, §9.3;
DECISIONS.md АР-25 п.6.

У ``feelings_app`` на ``safety_events`` только INSERT (0001): чтение таблицы приложению не
открывается, чтобы поля, которые могут появиться позже (служебные для протокола §9.3), не
стали читаемыми без пересмотра. Выгрузке нужны только тип и время своих событий — их отдаёт
функция.

``app_export_safety_events() RETURNS TABLE (type safety_event_type, created_at timestamptz)``
— SECURITY DEFINER, ``search_path = public, pg_temp``, STABLE, только для текущего пользователя
(``app_uid()``); нет входа, пользователь неизвестен или помечен ``deleted_at`` — ``42501``.
Порядок ``created_at, id``; ни ``id`` события, ни ``user_id`` не возвращаются. ``EXECUTE`` —
только ``feelings_app``.

Downgrade удаляет функцию.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXPORT_SAFETY_EVENTS = r"""
CREATE FUNCTION app_export_safety_events()
    RETURNS TABLE (type safety_event_type, created_at timestamptz)
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    #variable_conflict use_column
    DECLARE v_uid uuid := public.app_uid();
    BEGIN
        IF v_uid IS NULL THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        PERFORM 1 FROM public.users u WHERE u.id = v_uid AND u.deleted_at IS NULL;
        IF NOT FOUND THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        RETURN QUERY
        SELECT e.type, e.created_at FROM public.safety_events e
         WHERE e.user_id = v_uid
         ORDER BY e.created_at, e.id;
    END $f$
"""

UPGRADE_GRANTS = (
    "REVOKE ALL ON FUNCTION app_export_safety_events() FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION app_export_safety_events() TO feelings_app",
)


def upgrade() -> None:
    # По одной команде: asyncpg не выполняет несколько команд в одном запросе.
    op.execute(EXPORT_SAFETY_EVENTS)
    for stmt in UPGRADE_GRANTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app_export_safety_events()")

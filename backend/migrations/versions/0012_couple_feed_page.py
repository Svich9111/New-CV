"""Лента текущей пары страницами по (sent_at, id) (задача 3.3, A1).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-23 16:00:00

ФТ-10, ФТ-11; ТЗ §4.4 п.3–4, §4.5 п.2, §6.2 п.7, §7; DECISIONS.md АР-11, АР-25, АР-40.

``app_couple_feed(p_limit, p_before)`` (0001) отдаёт сообщения всех пар пользователя и режет по
``sent_at < p_before``: при одинаковом ``sent_at`` на границе пачки строки теряются, а фильтр
текущей пары приходилось делать в приложении добором пачек (отчёт A2, 3.3). Новая
``app_couple_feed_page(p_limit integer, p_before_at timestamptz, p_before_id uuid)``:

- только **текущая активная пара** пользователя (``couple_members.active`` и
  ``couples.status = 'active'``, как контекст 2.1 и захват памяти): после выхода или распада —
  пусто (ТЗ §4.5 п.2 «обмен прекращается»; история бывшей пары — в выгрузке, АР-25);
- порядок ``(sent_at DESC, id DESC)``, курсор — ключ последней показанной строки: строго
  «раньше» ``(p_before_at, p_before_id)``; оба NULL — первая страница; задан только один — 22023;
- автору — его оригинал, получателю — версия для получателя; недоставленные (``processing``,
  ``withheld``) получателю не видны (как ``app_couple_feed``, АР-11, АР-25 п.3);
- ``draft_id`` — в выдаче (контекст расшифровки оригинала автора, 4.2);
- предел 1–100 (NULL и ≤ 0 → 1); 42501 — нет ``app.user_id``, пользователь неизвестен или
  помечен удалённым.

Индекс ``ix_partner_messages_couple_sent (couple_id, sent_at, id)``. ``app_couple_feed`` (0001)
не меняется (на неё опирается выгрузка 0006). Downgrade удаляет функцию и индекс.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEED_PAGE = r"""
CREATE FUNCTION app_couple_feed_page(p_limit integer, p_before_at timestamptz, p_before_id uuid)
    RETURNS TABLE (id uuid, couple_id uuid, outgoing boolean, text_enc bytea, is_adapted boolean,
                   harsh_tone_warning boolean, status partner_message_status,
                   reply_to_id uuid, draft_id uuid, sent_at timestamptz, read_at timestamptz)
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
               m.sent_at, m.read_at
          FROM public.partner_messages m
         WHERE m.couple_id = v_couple
           AND (m.author_id = v_uid
                OR (m.recipient_id = v_uid AND m.status NOT IN ('processing', 'withheld')))
           AND (p_before_at IS NULL OR (m.sent_at, m.id) < (p_before_at, p_before_id))
         ORDER BY m.sent_at DESC, m.id DESC
         LIMIT v_lim;
    END $f$
"""

SIGNATURE = "app_couple_feed_page(integer, timestamp with time zone, uuid)"


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_partner_messages_couple_sent ON partner_messages (couple_id, sent_at, id)"
    )
    op.execute(FEED_PAGE)
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO feelings_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {SIGNATURE}")
    op.execute("DROP INDEX IF EXISTS ix_partner_messages_couple_sent")

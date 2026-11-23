"""Выгрузка сообщений пары для GET /me/export: app_export_partner_messages() (задача 4.5, этап 2, A1).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-21 23:50:00

ФТ-21, ФТ-10; ТЗ §2 п.3, §7 («оригинал отдаётся получателю отдельным запросом и фиксируется»),
§9.1 п.6, §9.3; DECISIONS.md АР-11, АР-13, АР-23, АР-25.

У ``feelings_app`` нет SELECT на ``partner_messages.original_enc`` (АР-11): оригинал отдают
только функции. Лента (``app_couple_feed``) отдаёт по 100 строк и одну версию текста; выгрузке
нужны все свои сообщения пары, у полученных — показанная версия и оригинал. Поэтому —
отдельная функция.

``app_export_partner_messages()`` — SECURITY DEFINER, ``search_path = public, pg_temp``,
только для текущего пользователя (``app_uid()``); нет пользователя, неизвестен или помечен
``deleted_at`` — ``42501``. Возвращает строки (порядок ``sent_at, id``):

- отправленные им (``outgoing = true``) во всех статусах: ``original_enc`` и ``draft_id`` (AAD
  оригинала — контекст черновика, ``crypto.MESSAGE_ORIGINAL``), статус, ``is_adapted``,
  ``harsh_tone_warning``, даты отправки, доставки, прочтения, ответа. Версии для получателя
  (``adapted_enc``) нет — её делал агент получателя (АР-25 п.3); ``shown_enc``,
  ``shown_is_original`` и ``original_viewed_at`` — NULL (просмотр оригинала — действие
  партнёра);
- полученные им (``outgoing = false``) только в статусах ``delivered``/``read``/``answered``
  (``processing`` и ``withheld`` получатель не видел, ТЗ §9.3): ``shown_enc`` — показанная
  версия, как в ленте (``adapted_enc``; если её нет — ``original_enc``, тогда
  ``shown_is_original = true``), ``original_enc`` и ``draft_id`` (черновик автора — только для
  AAD, в выгрузку не входит), флаги, даты и ``original_viewed_at``.

Шифротекст отдаётся как есть, ключа в БД нет; расшифровка — в приложении (``app.core.crypto``):
``original_enc`` и ``shown_enc`` при ``shown_is_original`` — ``MESSAGE_ORIGINAL`` с id =
``draft_id`` (NULL — код ``context``); ``shown_enc`` иначе — ``MESSAGE_ADAPTED`` с id = ``id``
сообщения. ``tone_score`` не возвращается никому.

Выдача оригинала фиксируется как просмотр (ТЗ §7, АР-25 п.2): у полученных
``original_viewed_at = COALESCE(original_viewed_at, now())``; ``original_viewed_at`` в
результате — уже с учётом этой выгрузки. Изменение и выборка — одной командой (одним снимком):
каждое возвращённое полученное сообщение зафиксировано.

Порядок блокировок (АР-13, АР-23: пользователь → пары → сообщения):

- строка пользователя ``FOR KEY SHARE`` — совместима с правкой профиля и отправкой, ждёт
  удаления аккаунта и выхода из пары (``FOR UPDATE``); после ожидания удалённого нет — ``42501``;
- текущая пара (pending/active) ``FOR KEY SHARE`` первой, затем все его пары, включая
  распавшиеся, ``ORDER BY id``: так же берёт пары удаление аккаунта (``app_leave_couple`` —
  текущую, затем все по id). Удаление аккаунта партнёра (``FOR UPDATE`` пар) и выгрузка идут
  по очереди, встречных блокировок сообщений нет; конвейер (``FOR KEY SHARE`` пары) с выгрузкой
  совместим;
- полученные сообщения без отметки просмотра — ``FOR UPDATE ORDER BY id`` до изменения.
  Конвейер, ставящий «отвечено» на полученное сообщение, держит только своё новое сообщение
  (выгрузка его не блокирует) — цикла нет.

Конфликт (40P01/40001) — 409 ``conflict_retry`` (АР-24). ``EXECUTE`` — только ``feelings_app``.

Downgrade удаляет функцию.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXPORT_PARTNER_MESSAGES = r"""
CREATE FUNCTION app_export_partner_messages()
    RETURNS TABLE (id uuid, couple_id uuid, outgoing boolean, draft_id uuid, reply_to_id uuid,
                   shown_enc bytea, shown_is_original boolean, original_enc bytea,
                   is_adapted boolean, harsh_tone_warning boolean,
                   status partner_message_status, sent_at timestamptz,
                   delivered_at timestamptz, read_at timestamptz, answered_at timestamptz,
                   original_viewed_at timestamptz)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    #variable_conflict use_column
    DECLARE v_uid uuid := public.app_uid();
    BEGIN
        IF v_uid IS NULL THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        -- АР-13: пользователь → пары → сообщения.
        PERFORM 1 FROM public.users u WHERE u.id = v_uid AND u.deleted_at IS NULL FOR KEY SHARE;
        IF NOT FOUND THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        -- Текущая пара первой, затем все по id — как app_delete_account (через app_leave_couple).
        PERFORM 1 FROM public.couples c
          JOIN public.couple_members cm ON cm.couple_id = c.id
         WHERE cm.user_id = v_uid AND cm.active FOR KEY SHARE OF c;
        PERFORM 1 FROM public.couples c
         WHERE c.id IN (SELECT cm.couple_id FROM public.couple_members cm
                         WHERE cm.user_id = v_uid)
         ORDER BY c.id FOR KEY SHARE;
        PERFORM 1 FROM public.partner_messages m
         WHERE m.recipient_id = v_uid AND m.status IN ('delivered', 'read', 'answered')
           AND m.original_viewed_at IS NULL
         ORDER BY m.id FOR UPDATE;
        -- Выдача оригинала фиксируется (ТЗ §7); изменение и выборка — одним снимком.
        RETURN QUERY
        WITH viewed AS (
            UPDATE public.partner_messages m SET original_viewed_at = now()
             WHERE m.recipient_id = v_uid AND m.status IN ('delivered', 'read', 'answered')
               AND m.original_viewed_at IS NULL
            RETURNING m.id, m.original_viewed_at)
        SELECT m.id, m.couple_id, m.author_id = v_uid, m.draft_id, m.reply_to_id,
               CASE WHEN m.author_id = v_uid THEN NULL
                    ELSE COALESCE(m.adapted_enc, m.original_enc) END,
               CASE WHEN m.author_id = v_uid THEN NULL ELSE m.adapted_enc IS NULL END,
               m.original_enc, m.is_adapted, m.harsh_tone_warning, m.status, m.sent_at,
               m.delivered_at, m.read_at, m.answered_at,
               CASE WHEN m.author_id = v_uid THEN NULL
                    ELSE COALESCE(v.original_viewed_at, m.original_viewed_at) END
          FROM public.partner_messages m
          LEFT JOIN viewed v ON v.id = m.id
         WHERE m.author_id = v_uid
            OR (m.recipient_id = v_uid AND m.status IN ('delivered', 'read', 'answered'))
         ORDER BY m.sent_at, m.id;
    END $f$
"""

UPGRADE_GRANTS = (
    "REVOKE ALL ON FUNCTION app_export_partner_messages() FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION app_export_partner_messages() TO feelings_app",
)


def upgrade() -> None:
    # По одной команде: asyncpg не выполняет несколько команд в одном запросе.
    op.execute(EXPORT_PARTNER_MESSAGES)
    for stmt in UPGRADE_GRANTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app_export_partner_messages()")

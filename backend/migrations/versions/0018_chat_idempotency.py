"""Личный чат: ключ идемпотентности реплики, связь ответа с репликой, флаг кризиса (2.2, A1).

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-26 21:00:00

ФТ-05, ФТ-17; ТЗ §7 (``conversation_messages``), §9.3, §10; DECISIONS.md АР-24, АР-32, АР-34,
АР-51. Ревью 2.2 р.1 MAJOR-1, MAJOR-2, MINOR-3, MINOR-4; предложения A4 1–3 (карточка 2.2).

Новые колонки ``conversation_messages`` (отступление от §7 — АР-51; прежние строки и вставки без
этих колонок остаются в силе):

- ``client_message_id`` (uuid, NULL) — ключ идемпотентности реплики **владельца**, его генерирует
  клиент (``POST /chat/messages``). Уникален в диалоге: частичный уникальный индекс
  ``uq_conversation_messages_client_message_id`` (``conversation_id, client_message_id`` WHERE
  ``client_message_id IS NOT NULL``). Уникальность в пределах владельца (все его диалоги) держит
  слой доступа: поиск ключа по всем диалогам владельца и вставка идут под блокировкой строки
  ``agents`` владельца (``FOR NO KEY UPDATE``, как и создание диалога, 2.1) — две одновременные
  попытки с одним ключом выполняются по очереди. Глобальной уникальности нет: одинаковый ключ у
  разных людей — разные реплики (и отказ по чужому ключу не выдавал бы чужую запись).
- ``reply_to_id`` (uuid, NULL) — у ответа **агента**: реплика владельца, на которую он отвечает.
  Составной внешний ключ ``(conversation_id, reply_to_id)`` → ``(conversation_id, id)`` (нужен
  уникальный ``uq_conversation_messages_conversation_id_id``): ответ и реплика — в одном диалоге,
  то есть у одного владельца (RLS 0001 ``conv_messages_owner``). ``ON DELETE CASCADE``.
  Частичный уникальный индекс ``uq_conversation_messages_reply_to_id``: на реплику — не больше
  одного сохранённого ответа.
- ``crisis`` (boolean, NOT NULL, false) — у ответа агента: это ответ кризисного протокола (§9.3);
  в истории клиент показывает под ним контакты помощи. Тип кризиса здесь не хранится (он — в
  ``safety_events`` без текста).
- ``reply_lease_until`` (timestamptz, NULL) — у реплики владельца с ключом: до этого времени ответ
  на неё готовит другой запрос (повтор с тем же ключом — 409 ``conflict_retry``); снимается по
  завершении запроса, сбойный процесс отпускает её по сроку.
- ``limit_charged`` (boolean, NOT NULL, false) — у реплики владельца с ключом: дневной лимит
  (АР-32) за неё уже засчитан; повтор с тем же ключом лимит повторно не расходует.

CHECK: ``owner_only_fields`` — ключ, аренда и отметка лимита только у ``author = 'owner'``;
``agent_only_fields`` — связь и флаг кризиса только у ``author = 'agent'``.

Прав и функций не добавляется: у ``feelings_app`` по-прежнему SELECT/INSERT/UPDATE/DELETE на
таблицу под политикой 0001 (только свои диалоги; табличные права покрывают новые колонки), у
остальных ролей прав на таблицу нет. Тексты — по-прежнему только ``text_enc`` (шифротекст).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

T = "conversation_messages"


def upgrade() -> None:
    op.add_column(T, sa.Column("client_message_id", sa.UUID(), nullable=True))
    op.add_column(T, sa.Column("reply_to_id", sa.UUID(), nullable=True))
    op.add_column(
        T, sa.Column("crisis", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )
    op.add_column(T, sa.Column("reply_lease_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        T,
        sa.Column("limit_charged", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_unique_constraint(
        "uq_conversation_messages_conversation_id_id", T, ["conversation_id", "id"]
    )
    op.create_foreign_key(
        "fk_conversation_messages_reply_to_id_conversation_messages",
        T,
        T,
        ["conversation_id", "reply_to_id"],
        ["conversation_id", "id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "uq_conversation_messages_client_message_id",
        T,
        ["conversation_id", "client_message_id"],
        unique=True,
        postgresql_where=sa.text("client_message_id IS NOT NULL"),
    )
    op.create_index(
        "uq_conversation_messages_reply_to_id",
        T,
        ["reply_to_id"],
        unique=True,
        postgresql_where=sa.text("reply_to_id IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_conversation_messages_owner_only_fields",
        T,
        "author = 'owner' OR (client_message_id IS NULL AND reply_lease_until IS NULL "
        "AND NOT limit_charged)",
    )
    op.create_check_constraint(
        "ck_conversation_messages_agent_only_fields",
        T,
        "author = 'agent' OR (reply_to_id IS NULL AND NOT crisis)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_conversation_messages_agent_only_fields", T, type_="check")
    op.drop_constraint("ck_conversation_messages_owner_only_fields", T, type_="check")
    op.drop_index(
        "uq_conversation_messages_reply_to_id",
        table_name=T,
        postgresql_where=sa.text("reply_to_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_conversation_messages_client_message_id",
        table_name=T,
        postgresql_where=sa.text("client_message_id IS NOT NULL"),
    )
    op.drop_constraint(
        "fk_conversation_messages_reply_to_id_conversation_messages", T, type_="foreignkey"
    )
    op.drop_constraint("uq_conversation_messages_conversation_id_id", T, type_="unique")
    for column in (
        "limit_charged",
        "reply_lease_until",
        "crisis",
        "reply_to_id",
        "client_message_id",
    ):
        op.drop_column(T, column)

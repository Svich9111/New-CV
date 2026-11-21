"""17 таблиц ТЗ §7. Комментарий у каждой таблицы ссылается на строку §7.

Связь «личные данные → владелец» обязательна: политики RLS (миграция 0001) опираются на
`users.id` через `app.user_id` текущей транзакции.
"""

import uuid
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import EMBEDDING_DIM, Base, created_at, uuid_pk


def _enum(*values: str, name: str) -> Enum:
    return Enum(*values, name=name, native_enum=True, create_constraint=False)


CoupleStatus = _enum("pending", "active", "dissolved", name="couple_status")
MemberRole = _enum("initiator", "partner", name="member_role")
InviteStatus = _enum("active", "used", "expired", "revoked", name="invite_status")
MessageAuthor = _enum("owner", "agent", name="message_author")
MemoryType = _enum(
    "fact",
    "date",
    "preference",
    "sensitive_topic",
    "observation",
    "from_partner",
    name="memory_type",
)
MemorySource = _enum("owner_dialog", "agent_inference", "partner_message", name="memory_source")
PartnerMessageStatus = _enum(
    "processing", "delivered", "read", "answered", name="partner_message_status"
)
DraftStatus = _enum("draft", "approved", "rejected", "blocked", "sent", name="draft_status")
SubscriptionStatus = _enum("trial", "active", "expired", "canceled", name="subscription_status")
PaymentStatus = _enum("pending", "succeeded", "failed", "refunded", name="payment_status")
IndexStatus = _enum("uploaded", "indexing", "indexed", "failed", name="kb_index_status")


class User(Base):
    """§7 users: id, telegram_id, имя, обращение, часовой пояс, согласия, даты создания/удаления."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(64))
    address_form: Mapped[str | None] = mapped_column(String(64))  # «как обращаться»
    # ФТ-02 / ТЗ §4.1 п.2: имя партнёра спрашивается в онбординге (поле сверх §7, см. АР-6).
    partner_name: Mapped[str | None] = mapped_column(String(64))
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="Europe/Moscow"
    )
    consent_terms_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_data_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_sensitive_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    age_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Couple(Base):
    """§7 couples: id, статус (ожидает / активна / расторгнута), даты."""

    __tablename__ = "couples"

    id: Mapped[uuid.UUID] = uuid_pk()
    status: Mapped[str] = mapped_column(CoupleStatus, nullable=False, server_default="pending")
    created_at: Mapped[datetime] = created_at()
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dissolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CoupleMember(Base):
    """§7 couple_members: couple_id, user_id, роль. Ровно две записи на активную пару.

    unique(couple_id, role) при двух ролях даёт не более двух участников;
    частичный уникальный индекс по user_id WHERE active — одна активная пара (ФТ-04).
    """

    __tablename__ = "couple_members"
    __table_args__ = (
        UniqueConstraint("couple_id", "role", name="uq_couple_members_couple_role"),
        UniqueConstraint("couple_id", "user_id", name="uq_couple_members_couple_user"),
        Index(
            "uq_couple_members_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=sa_text("active"),
        ),
    )

    couple_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("couples.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(MemberRole, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa_text("true"))
    joined_at: Mapped[datetime] = created_at()
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Invite(Base):
    """§7 invites: токен, couple_id, создатель, срок действия, статус.

    Хранится только SHA-256 токена: утечка БД не даёт рабочих ссылок.
    """

    __tablename__ = "invites"

    id: Mapped[uuid.UUID] = uuid_pk()
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True, nullable=False)
    couple_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("couples.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(InviteStatus, nullable=False, server_default="active")
    used_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()


class PromptVersion(Base):
    """§7 prompt_versions: id, назначение, текст, дата, признак активной."""

    __tablename__ = "prompt_versions"
    __table_args__ = (
        Index(
            "uq_prompt_versions_one_active",
            "purpose",
            unique=True,
            postgresql_where=sa_text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at()
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )


class Agent(Base):
    """§7 agents: id, user_id, версия промпта, сводка профиля владельца, настройки тона."""

    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    prompt_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("prompt_versions.id", ondelete="SET NULL")
    )
    profile_summary_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    tone_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = created_at()


class Conversation(Base):
    """§7 conversations: id, agent_id, начало, краткое содержание."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = uuid_pk()
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = created_at()
    summary_enc: Mapped[bytes | None] = mapped_column(LargeBinary)


class ConversationMessage(Base):
    """§7 conversation_messages: id, conversation_id, автор, текст (зашифрован), время."""

    __tablename__ = "conversation_messages"

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author: Mapped[str] = mapped_column(MessageAuthor, nullable=False)
    text_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = created_at()


class MemoryItem(Base):
    """§7 memory_items: id, agent_id, тип, текст, дата события, источник, уверенность, вектор,
    признак удаления. §6.3: удалённая запись не извлекается повторно — `fingerprint`
    (хеш нормализованного текста) остаётся после удаления, текст стирается."""

    __tablename__ = "memory_items"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint(
            "deleted_at IS NULL OR (text_enc IS NULL AND embedding IS NULL)",
            name="deleted_has_no_content",
        ),
        CheckConstraint(
            "(type = 'from_partner') = (source = 'partner_message')",
            name="partner_source_consistent",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(MemoryType, nullable=False)
    text_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    event_date: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(MemorySource, nullable=False)
    # Ссылка на одобренное сообщение пары, если source = partner_message (ФТ-14).
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("partner_messages.id", ondelete="SET NULL")
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default=sa_text("1.0"))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    fingerprint: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PartnerMessage(Base):
    """§7 partner_messages: id, couple_id, автор, получатель, оригинал, адаптированная версия,
    признак адаптации, оценка тона, статус, время отправки и прочтения, факт просмотра оригинала.

    Строка появляется только после нажатия «Отправить» (ФТ-08, ТЗ §2 п.2)."""

    __tablename__ = "partner_messages"
    __table_args__ = (
        CheckConstraint("author_id <> recipient_id", name="author_not_recipient"),
        CheckConstraint("tone_score IS NULL OR tone_score BETWEEN 0 AND 3", name="tone_range"),
        CheckConstraint("NOT is_adapted OR adapted_enc IS NOT NULL", name="adapted_has_text"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    couple_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("couples.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    reply_to_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("partner_messages.id", ondelete="SET NULL")
    )
    original_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    adapted_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    is_adapted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    tone_score: Mapped[int | None] = mapped_column(SmallInteger)
    # §6.2 п.6: при расхождении смысла передаётся оригинал с предупреждением о резком тоне.
    harsh_tone_warning: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    status: Mapped[str] = mapped_column(
        PartnerMessageStatus, nullable=False, server_default="processing"
    )
    sent_at: Mapped[datetime] = created_at()
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    original_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MessageDraft(Base):
    """§7 message_drafts: id, agent_id, текст, версия, статус. Видны только автору."""

    __tablename__ = "message_drafts"

    id: Mapped[uuid.UUID] = uuid_pk()
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("1"))
    status: Mapped[str] = mapped_column(DraftStatus, nullable=False, server_default="draft")
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KbDocument(Base):
    """§7 kb_documents: id, название, автор, подход, основание прав, статус индексации."""

    __tablename__ = "kb_documents"

    id: Mapped[uuid.UUID] = uuid_pk()
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    author: Mapped[str | None] = mapped_column(String(200))
    approach: Mapped[str | None] = mapped_column(String(100))
    rights_basis: Mapped[str] = mapped_column(Text, nullable=False)  # §6.4: только с правами
    index_status: Mapped[str] = mapped_column(
        IndexStatus, nullable=False, server_default="uploaded"
    )
    created_at: Mapped[datetime] = created_at()


class KbChunk(Base):
    """§7 kb_chunks: id, document_id, текст, метки темы и уровня, вектор."""

    __tablename__ = "kb_chunks"
    __table_args__ = (
        CheckConstraint(
            "problem_level IS NULL OR problem_level BETWEEN 1 AND 5", name="level_range"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    topic_tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, server_default=sa_text("'{}'")
    )
    problem_level: Mapped[int | None] = mapped_column(SmallInteger)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))


class SafetyEvent(Base):
    """§7 safety_events: id, user_id, тип, время. Без текста переписки (§9.3)."""

    __tablename__ = "safety_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = created_at()


class Subscription(Base):
    """§7 subscriptions: id, couple_id, плательщик, тариф, статус, период."""

    __tablename__ = "subscriptions"
    __table_args__ = (CheckConstraint("period_end > period_start", name="period_order"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    couple_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("couples.id", ondelete="CASCADE"), nullable=False, index=True
    )
    payer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    plan: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(SubscriptionStatus, nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = created_at()


class Payment(Base):
    """§7 payments: id, subscription_id, сумма, провайдер, статус, время."""

    __tablename__ = "payments"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # в минимальных единицах / Stars
    currency: Mapped[str] = mapped_column(String(8), nullable=False, server_default="XTR")
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_charge_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(PaymentStatus, nullable=False)
    created_at: Mapped[datetime] = created_at()


class AuditLog(Base):
    """§7 audit_log: id, кто, действие, объект, время. Без содержимого сообщений."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at()

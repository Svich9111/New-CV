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
# withheld — остановлено проверкой безопасности (ТЗ §6.2 п.3, §9.3): получатель не видит.
PartnerMessageStatus = _enum(
    "processing", "withheld", "delivered", "read", "answered", name="partner_message_status"
)
DraftStatus = _enum("draft", "approved", "rejected", "blocked", "sent", name="draft_status")
SubscriptionStatus = _enum("trial", "active", "expired", "canceled", name="subscription_status")
PaymentStatus = _enum("pending", "succeeded", "failed", "refunded", name="payment_status")
SafetyEventType = _enum(
    "violence",
    "threat_outgoing",
    "self_harm",
    "life_threat",
    "underage",
    name="safety_event_type",
)
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
    __table_args__ = (
        Index(
            "uq_invites_one_active_per_couple",
            "couple_id",
            unique=True,
            postgresql_where=sa_text("status = 'active'"),
        ),
    )

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
        UniqueConstraint("purpose", "version", name="uq_prompt_versions_purpose_version"),
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
    """§7 agents: id, user_id, версия промпта, сводка профиля владельца, настройки тона.

    Служебные колонки ``memory_*`` (0009, АР-36) — состояние фоновых задач памяти: отметка
    обработанных сообщений пары, аренда воркера, время последнего сжатия. Меняют их только
    функции БД (у приложения нет прав на эти колонки)."""

    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint(
            "(memory_partner_through_at IS NULL) = (memory_partner_through_id IS NULL)",
            name="memory_partner_through_pair",
        ),
        CheckConstraint(
            "memory_lease_token IS NULL OR (memory_lease_kind IS NOT NULL "
            "AND memory_lease_at IS NOT NULL AND memory_lease_until IS NOT NULL)",
            name="memory_lease_whole",
        ),
        CheckConstraint(
            "memory_lease_kind IS NULL OR memory_lease_kind IN ('extract', 'compact')",
            name="memory_lease_kind",
        ),
        CheckConstraint("memory_attempts >= 0", name="memory_attempts_nonnegative"),
    )

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
    # ФТ-14: доставленные владельцу сообщения пары обработаны извлечением по (delivered_at, id).
    memory_partner_through_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    memory_partner_through_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    memory_compacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 0010 (АР-37 п.1): удаление записи владельцем — сжатие вне периода; снимает сжатие.
    memory_compaction_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    memory_lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    memory_lease_kind: Mapped[str | None] = mapped_column(String(8))
    memory_lease_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    memory_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    memory_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0")
    )


class Conversation(Base):
    """§7 conversations: id, agent_id, начало, краткое содержание.

    ``memory_through_at/_id`` (0009, АР-36) — реплика, по которую включительно диалог обработан
    извлечением памяти; меняет только ``app_memory_extraction_done``."""

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(
            "(memory_through_at IS NULL) = (memory_through_id IS NULL)",
            name="memory_through_pair",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = created_at()
    summary_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    memory_through_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    memory_through_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class ConversationMessage(Base):
    """§7 conversation_messages: id, conversation_id, автор, текст (зашифрован), время."""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        # Порядок реплик (created_at, id): сессии и отметки извлечения (0009), сводка (2.1).
        Index("ix_conversation_messages_conversation_order", "conversation_id", "created_at", "id"),
    )

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
        CheckConstraint(
            "source <> 'partner_message' OR source_message_id IS NOT NULL",
            name="partner_source_has_message",
        ),
        # 0010 (Б-68): у записи с текстом всегда есть отпечаток — удаление оставляет след.
        CheckConstraint("text_enc IS NULL OR fingerprint IS NOT NULL", name="text_has_fingerprint"),
        # 0008 (АР-35): имя модели без вектора не хранится; удаление обнуляет оба поля.
        CheckConstraint(
            "embedding_model IS NULL OR (embedding IS NOT NULL AND embedding_model <> '')",
            name="model_has_vector",
        ),
        Index(
            "ix_memory_items_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # 0009 (АР-36): проверка отпечатков удалённых записей своего агента.
        Index(
            "ix_memory_items_agent_fingerprint",
            "agent_id",
            "fingerprint",
            postgresql_where=sa_text("fingerprint IS NOT NULL"),
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
        ForeignKey("partner_messages.id", ondelete="CASCADE")
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default=sa_text("1.0"))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    # АР-5, АР-35: модель, которой сделан вектор; поиск — только по векторам текущей модели.
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    # АР-36: HMAC-SHA-256 нормализованного текста (считает приложение); после удаления остаётся.
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
        # 0012: лента текущей пары страницами по (sent_at, id) — app_couple_feed_page.
        Index("ix_partner_messages_couple_sent", "couple_id", "sent_at", "id"),
        # 0009: доставленные получателю по порядку (delivered_at, id) — извлечение памяти (ФТ-14).
        Index(
            "ix_partner_messages_recipient_delivered",
            "recipient_id",
            "delivered_at",
            "id",
            postgresql_where=sa_text("delivered_at IS NOT NULL"),
        ),
        # 0013 (АР-44): причина остановки — только коды и только у withheld; аренда целиком.
        CheckConstraint(
            "withheld_reason IS NULL OR withheld_reason IN ('safety', 'model_refusal', "
            "'pipeline_error', 'attempts_exhausted', 'couple_ended')",
            name="withheld_reason_code",
        ),
        CheckConstraint(
            "withheld_reason IS NULL OR status = 'withheld'", name="withheld_reason_status"
        ),
        # 0014 (АР-45): срок без токена — «не захватывать раньше» (pipeline_release).
        CheckConstraint(
            "work_lease_token IS NULL OR work_lease_until IS NOT NULL", name="work_lease_has_until"
        ),
        # 0014 (АР-45): тип кризиса — только у остановленного безопасностью, не «младше 18».
        CheckConstraint(
            "withheld_crisis IS NULL OR (withheld_reason = 'safety' "
            "AND withheld_crisis <> 'underage')",
            name="withheld_crisis_safety",
        ),
        CheckConstraint("work_attempts >= 0", name="work_attempts_nonnegative"),
        # 0013: очередь конвейера (processing) и очередь уведомлений получателю (Б-47).
        Index(
            "ix_partner_messages_processing",
            "sent_at",
            "id",
            postgresql_where=sa_text("status = 'processing'"),
        ),
        Index(
            "ix_partner_messages_notify",
            "delivered_at",
            "id",
            postgresql_where=sa_text("status = 'delivered' AND notify_done_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    couple_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("couples.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Удаление аккаунта получателя не стирает сообщения, которые написал партнёр (ФТ-21 vs Д7,
    # окончательно — задача 4.5): получатель обнуляется.
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # Одобренный автором черновик, из которого создано сообщение (ФТ-08): без него строки нет.
    draft_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("message_drafts.id", ondelete="SET NULL", use_alter=True), unique=True
    )
    reply_to_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("partner_messages.id", ondelete="SET NULL")
    )
    original_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Версия для получателя (ТЗ §4.3 п.3): адаптированная или копия оригинала; заполняет конвейер.
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
    # Служебные колонки конвейера вне §7 (0013, АР-44; как АР-36): причина остановки — видна
    # автору; аренда сообщения воркером (processing — конвейер, delivered — уведомление);
    # notify_done_at — уведомление получателю отправлено или не нужно. Меняют только функции.
    withheld_reason: Mapped[str | None] = mapped_column(String(32))
    # 0014: главный тип кризиса при withheld_reason = safety — протокол автору (§9.3).
    withheld_crisis: Mapped[str | None] = mapped_column(SafetyEventType)
    work_lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    work_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    work_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    notify_done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MessageDraft(Base):
    """§7 message_drafts: id, agent_id, текст, версия, статус. Видны только автору."""

    __tablename__ = "message_drafts"

    id: Mapped[uuid.UUID] = uuid_pk()
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Ответ на конкретное входящее (ФТ-11 «отвечено», ключевой сценарий §1).
    reply_to_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("partner_messages.id", ondelete="SET NULL")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("1"))
    status: Mapped[str] = mapped_column(DraftStatus, nullable=False, server_default="draft")
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KbDocument(Base):
    """§7 kb_documents: id, название, автор, подход, основание прав, статус индексации.

    0011 (АР-39): исходный файл (``source_*``) — только до конца индексации; ``index_error`` —
    код причины сбоя без текста; аренда воркера индексации (``index_lease_*``, попытки)."""

    __tablename__ = "kb_documents"
    __table_args__ = (
        CheckConstraint(
            "source_data IS NULL OR index_status IN ('uploaded', 'indexing')",
            name="source_only_before_index",
        ),
        CheckConstraint(
            "source_data IS NULL OR octet_length(source_data) BETWEEN 1 AND 20971520",
            name="source_size",
        ),
        CheckConstraint(
            "source_format IS NULL OR source_format IN ('pdf', 'epub', 'docx', 'txt')",
            name="source_format",
        ),
        CheckConstraint(
            "source_sha256 IS NULL OR octet_length(source_sha256) = 32", name="sha256_len"
        ),
        CheckConstraint(
            "index_error IS NULL OR index_error ~ '^[a-z][a-z0-9_]{0,31}$'", name="error_code"
        ),
        CheckConstraint("btrim(rights_basis) <> ''", name="rights_basis_present"),
        CheckConstraint("index_attempts >= 0", name="attempts_nonnegative"),
        Index(
            "uq_kb_documents_sha256_active",
            "source_sha256",
            unique=True,
            postgresql_where=sa_text("source_sha256 IS NOT NULL AND index_status <> 'failed'"),
        ),
        Index(
            "ix_kb_documents_index_queue",
            "created_at",
            "id",
            postgresql_where=sa_text("index_status IN ('uploaded', 'indexing')"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    author: Mapped[str | None] = mapped_column(String(200))
    approach: Mapped[str | None] = mapped_column(String(100))
    rights_basis: Mapped[str] = mapped_column(Text, nullable=False)  # §6.4: только с правами
    index_status: Mapped[str] = mapped_column(
        IndexStatus, nullable=False, server_default="uploaded"
    )
    created_at: Mapped[datetime] = created_at()
    source_data: Mapped[bytes | None] = mapped_column(LargeBinary)
    source_format: Mapped[str | None] = mapped_column(String(8))
    source_size: Mapped[int | None] = mapped_column(Integer)
    source_sha256: Mapped[bytes | None] = mapped_column(LargeBinary)
    index_error: Mapped[str | None] = mapped_column(String(32))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    index_lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    index_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    index_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0")
    )


class KbChunk(Base):
    """§7 kb_chunks: id, document_id, текст, метки темы и уровня, вектор."""

    __tablename__ = "kb_chunks"
    __table_args__ = (
        CheckConstraint(
            "problem_level IS NULL OR problem_level BETWEEN 1 AND 5", name="level_range"
        ),
        CheckConstraint(
            "embedding_model IS NULL OR (embedding IS NOT NULL AND embedding_model <> '')",
            name="model_has_vector",
        ),
        CheckConstraint("ord IS NULL OR ord >= 0", name="ord_nonnegative"),
        # 0011: порядок фрагмента в материале.
        Index("uq_kb_chunks_document_ord", "document_id", "ord", unique=True),
        Index(
            "ix_kb_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
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
    ord: Mapped[int | None] = mapped_column(Integer)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(128))


class SafetyEvent(Base):
    """§7 safety_events: id, user_id, тип, время. Без текста переписки (§9.3)."""

    __tablename__ = "safety_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(SafetyEventType, nullable=False)
    created_at: Mapped[datetime] = created_at()


class Subscription(Base):
    """§7 subscriptions: id, couple_id, плательщик, тариф, статус, период."""

    __tablename__ = "subscriptions"
    # Строка — период доступа пары: пробный (plan 'trial') или оплаченный; возврат — canceled.
    # Пробный — один раз на пару (миграция 0017, АР-50).
    __table_args__ = (
        CheckConstraint("period_end > period_start", name="period_order"),
        Index(
            "uq_subscriptions_trial_per_couple",
            "couple_id",
            unique=True,
            postgresql_where=sa_text("plan = 'trial'"),
        ),
    )

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
    # Счёт — строка pending без периода (id — payload счёта Telegram); зачисление — succeeded с
    # периодом и telegram_payment_charge_id; возврат — refunded (миграция 0017, АР-50).
    # Личных данных нет: плательщик — subscriptions.payer_id.
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(
            "couple_id IS NOT NULL OR subscription_id IS NOT NULL", name="couple_or_subscription"
        ),
        CheckConstraint(
            "status <> 'pending' OR subscription_id IS NOT NULL OR (couple_id IS NOT NULL "
            "AND plan IS NOT NULL AND period_days IS NOT NULL AND expires_at IS NOT NULL)",
            name="invoice_complete",
        ),
        CheckConstraint(
            "period_days IS NULL OR period_days BETWEEN 1 AND 400", name="period_days_range"
        ),
        CheckConstraint("refunded_at IS NULL OR status = 'refunded'", name="refunded_at_status"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_code"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True
    )
    couple_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("couples.id", ondelete="CASCADE"), index=True
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # в минимальных единицах / Stars
    currency: Mapped[str] = mapped_column(String(8), nullable=False, server_default="XTR")
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # telegram_payment_charge_id: идемпотентность зачисления и возврат (refundStarPayment).
    provider_charge_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(PaymentStatus, nullable=False)
    plan: Mapped[str | None] = mapped_column(String(32))  # тариф счёта: month / year
    period_days: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # срок счёта
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()


class AuditLog(Base):
    """§7 audit_log: id, кто, действие, объект, время. Без содержимого сообщений."""

    __tablename__ = "audit_log"
    # Только машинные коды — свободный текст (и содержимое сообщений) записать нельзя (§7).
    __table_args__ = (
        CheckConstraint("action ~ '^[a-z][a-z0-9_.]{0,63}$'", name="action_code"),
        CheckConstraint("object_type ~ '^[a-z][a-z0-9_]{0,63}$'", name="object_type_code"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at()

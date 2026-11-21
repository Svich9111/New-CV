"""Модель данных — ТЗ, раздел 7 (17 таблиц). Владелец: A1.

Тексты личных данных (диалоги, память, черновики, сообщения пары) хранятся только в
зашифрованном виде — колонки `*_enc` типа bytea (ТЗ §9.1 п.3). Шифрование на уровне
приложения — задача 4.2; схема готова к нему с первого дня.
"""

from app.models.base import EMBEDDING_DIM, Base
from app.models.tables import (
    Agent,
    AuditLog,
    Conversation,
    ConversationMessage,
    Couple,
    CoupleMember,
    Invite,
    KbChunk,
    KbDocument,
    MemoryItem,
    MessageDraft,
    PartnerMessage,
    Payment,
    PromptVersion,
    SafetyEvent,
    Subscription,
    User,
)

__all__ = [
    "EMBEDDING_DIM",
    "Agent",
    "AuditLog",
    "Base",
    "Conversation",
    "ConversationMessage",
    "Couple",
    "CoupleMember",
    "Invite",
    "KbChunk",
    "KbDocument",
    "MemoryItem",
    "MessageDraft",
    "PartnerMessage",
    "Payment",
    "PromptVersion",
    "SafetyEvent",
    "Subscription",
    "User",
]

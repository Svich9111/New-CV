"""Схемы API — зеркало components.schemas из api/openapi.yaml."""

import unicodedata
import uuid
from datetime import date, datetime
from functools import cache
from importlib import resources
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
)


class Me(BaseModel):
    id: uuid.UUID
    name: str | None
    address_form: str | None
    partner_name: str | None
    timezone: str
    onboarding_completed: bool
    has_active_couple: bool


class TelegramAuthRequest(BaseModel):
    init_data: str = Field(min_length=1, max_length=8192)


class Session(BaseModel):
    access_token: str
    expires_in: int
    me: Me


# Запрещённые внутри строк категории Unicode: управляющие (C0, C1), формата (невидимые,
# смена направления текста, BOM), разделители строк и абзацев. Имена попадут в промпт и
# интерфейс — перевод строки или U+202E не должны проходить «в обход» правила.
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
# Исключение из Cf: ZWJ и ZWNJ нужны эмодзи-последовательностям и ряду письменностей.
_ALLOWED_FORMAT = frozenset({"\u200c", "\u200d"})
# Служебный файл базы tz, а не зона.
_NOT_ZONES = frozenset({"Factory"})


def _plain_text(value: str) -> str:
    visible = False
    for ch in value:
        category = unicodedata.category(ch)
        if category in _FORBIDDEN_CATEGORIES and ch not in _ALLOWED_FORMAT:
            raise ValueError("control or format characters are not allowed")
        if category[0] not in "CZ":
            visible = True
    if not visible:
        raise ValueError("value must contain visible characters")
    return value


@cache
def _iana_zones() -> frozenset[str]:
    """Список зон только из пакета tzdata, а не из системного каталога: набор одинаков на всех
    машинах и не включает posix/…, right/… и служебные файлы."""
    names = resources.files("tzdata").joinpath("zones").read_text(encoding="utf-8").split()
    return frozenset(names) - _NOT_ZONES


def _iana_zone(value: str) -> str:
    if value not in _iana_zones():
        raise ValueError("unknown time zone")
    return value


def _must_be_true(value: bool) -> bool:
    if value is not True:
        raise ValueError("consent is required")
    return value


ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    AfterValidator(_plain_text),
]
TimeZone = Annotated[str, StringConstraints(max_length=64), AfterValidator(_iana_zone)]
# Контракт: {type: boolean, const: true}. Только JSON true: 1, 1.0, "true" — 422.
Consent = Annotated[StrictBool, AfterValidator(_must_be_true)]


class OnboardingRequest(BaseModel):
    """ФТ-02. Три согласия — const true по контракту: без любого из них — 422.
    age_confirmed=false — не ошибка схемы, а отказ в доступе 403 underage (ТЗ §9.3)."""

    model_config = ConfigDict(strict=True)

    name: ShortText
    address_form: ShortText
    partner_name: ShortText
    timezone: TimeZone = "Europe/Moscow"
    consent_terms: Consent
    consent_data: Consent
    consent_sensitive: Consent
    age_confirmed: StrictBool


class CoupleMember(BaseModel):
    role: Literal["initiator", "partner"]
    name: str | None
    is_me: bool


class Couple(BaseModel):
    id: uuid.UUID
    status: Literal["pending", "active", "dissolved"]
    members: list[CoupleMember] = Field(max_length=2)


class InviteCreated(BaseModel):
    couple_id: uuid.UUID
    link: str
    expires_at: datetime


class InvitePreview(BaseModel):
    """GET /invites/{token}: только имя пригласившего и срок — ни пары, ни участников."""

    inviter_name: str | None
    expires_at: datetime


# --- Выгрузка своих данных GET /me/export (ФТ-21; АР-25) ------------------------------------


class _Export(BaseModel):
    """Состав полей — ровно как в контракте (`additionalProperties: false`)."""

    model_config = ConfigDict(extra="forbid")


class UnavailableText(_Export):
    """Текст не расшифровался: только код причины `DecryptionError.reason`, без текста."""

    unavailable: Literal[True] = True
    reason: Literal["format", "unknown_key", "auth", "encoding", "context"]


ExportText = str | UnavailableText


class ExportProfile(_Export):
    id: uuid.UUID
    telegram_id: int
    name: str | None
    address_form: str | None
    partner_name: str | None
    timezone: str
    created_at: datetime
    consent_terms_at: datetime | None
    consent_data_at: datetime | None
    consent_sensitive_at: datetime | None
    age_confirmed_at: datetime | None
    onboarding_completed: bool


class ExportAgent(_Export):
    created_at: datetime
    profile_summary: ExportText | None
    tone_settings: dict[str, Any]


class ExportConversationMessage(_Export):
    id: uuid.UUID
    author: Literal["owner", "agent"]
    text: ExportText
    created_at: datetime


class ExportConversation(_Export):
    id: uuid.UUID
    started_at: datetime
    summary: ExportText | None
    messages: list[ExportConversationMessage]


class ExportMemoryItem(_Export):
    id: uuid.UUID
    type: Literal["fact", "date", "preference", "sensitive_topic", "observation", "from_partner"]
    text: ExportText
    event_date: date | None
    source: Literal["owner_dialog", "agent_inference", "partner_message"]
    source_message_id: uuid.UUID | None
    confidence: float
    created_at: datetime
    updated_at: datetime | None


class ExportDraft(_Export):
    id: uuid.UUID
    text: ExportText
    reply_to_id: uuid.UUID | None
    version: int
    status: Literal["draft", "approved", "rejected", "blocked", "sent"]
    created_at: datetime
    updated_at: datetime | None


class ExportSentMessage(_Export):
    """Своё сообщение: оригинал и статус; версия для получателя не выгружается (АР-25 п.3)."""

    id: uuid.UUID
    couple_id: uuid.UUID
    reply_to_id: uuid.UUID | None
    original: ExportText
    is_adapted: bool
    harsh_tone_warning: bool
    status: Literal["processing", "withheld", "delivered", "read", "answered"]
    sent_at: datetime
    delivered_at: datetime | None
    read_at: datetime | None
    answered_at: datetime | None


class ExportReceivedMessage(_Export):
    """Доставленное сообщение партнёра: показанная версия и оригинал (АР-25 п.1–2). id
    черновика партнёра (нужен только для расшифровки) сюда не входит."""

    id: uuid.UUID
    couple_id: uuid.UUID
    reply_to_id: uuid.UUID | None
    text: ExportText
    is_adapted: bool
    harsh_tone_warning: bool
    original: ExportText
    original_viewed_at: datetime
    status: Literal["delivered", "read", "answered"]
    sent_at: datetime
    read_at: datetime | None


class ExportCoupleMessages(_Export):
    sent: list[ExportSentMessage]
    received: list[ExportReceivedMessage]


class ExportCouple(_Export):
    id: uuid.UUID
    status: Literal["pending", "active", "dissolved"]
    role: Literal["initiator", "partner"]
    created_at: datetime
    activated_at: datetime | None
    dissolved_at: datetime | None
    joined_at: datetime
    left_at: datetime | None


class ExportPayment(_Export):
    id: uuid.UUID
    amount: int
    currency: str
    provider: str
    provider_charge_id: str | None
    status: Literal["pending", "succeeded", "failed", "refunded"]
    created_at: datetime


class ExportSubscription(_Export):
    id: uuid.UUID
    couple_id: uuid.UUID
    plan: str
    status: Literal["trial", "active", "expired", "canceled"]
    period_start: datetime
    period_end: datetime
    created_at: datetime
    payments: list[ExportPayment]


class ExportSafetyEvent(_Export):
    type: Literal["violence", "threat_outgoing", "self_harm", "life_threat", "underage"]
    created_at: datetime


class AccountExport(_Export):
    export_version: Literal[1] = 1
    exported_at: datetime
    profile: ExportProfile
    agent: ExportAgent | None
    conversations: list[ExportConversation]
    memory: list[ExportMemoryItem]
    drafts: list[ExportDraft]
    couple_messages: ExportCoupleMessages
    couples: list[ExportCouple]
    subscriptions: list[ExportSubscription]
    safety_events: list[ExportSafetyEvent]


# --- Личный чат (задача 2.1, этап 2; контракт `/chat/messages`, АР-32) ---------------------


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("пустая реплика")
    return value


class ChatMessageRequest(BaseModel):
    """Тело `POST /chat/messages`: `text`, 1–4000 символов, не из одних пробелов; необязательный
    ключ идемпотентности `client_message_id` (uuid строкой, АР-51)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    text: Annotated[
        str, StringConstraints(min_length=1, max_length=4000), AfterValidator(_not_blank)
    ]
    # Строгий режим не принимает uuid строкой из JSON — только для этого поля он снят.
    client_message_id: Annotated[uuid.UUID | None, Field(strict=False)] = None


class ChatMessage(_Export):
    id: uuid.UUID
    author: Literal["owner", "agent"]
    text: ExportText
    created_at: datetime
    client_message_id: uuid.UUID | None  # только у реплики владельца, отправленной с ключом
    reply_to: uuid.UUID | None  # у ответа агента: реплика владельца, на которую он отвечает
    crisis: bool  # ответ кризисного протокола (у реплик владельца — false)


class ChatPage(_Export):
    items: list[ChatMessage]
    next_cursor: str | None


class HelpContact(BaseModel):
    name: str
    phone: str
    description: str
    round_the_clock: bool | None = None


# --- Память агента (задача 2.4, этап 2; контракт `/memory`, MemoryItem) --------------------

MemoryTypeName = Literal[
    "fact", "date", "preference", "sensitive_topic", "observation", "from_partner"
]


class MemoryItem(BaseModel):
    id: uuid.UUID
    type: MemoryTypeName
    text: ExportText  # строка или `UnavailableText` — запись видна и удаляется
    event_date: date | None
    source: Literal["owner_dialog", "agent_inference", "partner_message"]
    confidence: float = Field(ge=0, le=1)
    created_at: datetime


class MemoryPage(BaseModel):
    items: list[MemoryItem]

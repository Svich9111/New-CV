"""Схемы API — зеркало components.schemas из api/openapi.yaml."""

import unicodedata
import uuid
from datetime import datetime
from functools import cache
from importlib import resources
from typing import Annotated, Literal

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

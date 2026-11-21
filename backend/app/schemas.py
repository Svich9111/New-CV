"""Схемы API — зеркало components.schemas из api/openapi.yaml."""

import re
import uuid
import zoneinfo
from functools import cache
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints


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


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Служебные файлы базы tz, а не зоны.
_NOT_ZONES = frozenset({"localtime", "posixrules", "Factory"})


def _no_control_chars(value: str) -> str:
    if _CONTROL.search(value):
        raise ValueError("control characters are not allowed")
    return value


@cache
def _iana_zones() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones()) - _NOT_ZONES


def _iana_zone(value: str) -> str:
    if value not in _iana_zones():
        raise ValueError("unknown time zone")
    return value


ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    AfterValidator(_no_control_chars),
]
TimeZone = Annotated[str, StringConstraints(max_length=64), AfterValidator(_iana_zone)]


class OnboardingRequest(BaseModel):
    """ФТ-02. Три согласия — const true по контракту: без любого из них — 422.
    age_confirmed=false — не ошибка схемы, а отказ в доступе 403 underage (ТЗ §9.3)."""

    model_config = ConfigDict(strict=True)

    name: ShortText
    address_form: ShortText
    partner_name: ShortText
    timezone: TimeZone = "Europe/Moscow"
    consent_terms: Literal[True]
    consent_data: Literal[True]
    consent_sensitive: Literal[True]
    age_confirmed: bool

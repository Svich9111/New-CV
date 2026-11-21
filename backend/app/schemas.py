"""Схемы API — зеркало components.schemas из api/openapi.yaml."""

import uuid

from pydantic import BaseModel, Field


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

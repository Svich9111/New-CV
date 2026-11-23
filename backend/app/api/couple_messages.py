"""Лента пары, оригинал, «прочитано» — `GET /couple/messages`,
`GET /couple/messages/{id}/original`, `POST /couple/messages/{id}/read` (ФТ-10, ФТ-11; ТЗ §2 п.3,
§4.4 п.3–4, §6.2 п.7, §7; задача 3.3; АР-11, АР-25).

Требует завершённого онбординга (403 `onboarding_required`), как остальные пути пары (`/couple`):
переписка пары — обработка чувствительных данных по согласию; после отказа по возрасту доступа
нет (ТЗ §9.3). Свои данные без онбординга — `GET /me/export` (АР-25).

Только текущая активная пара (`app_couple_feed_page`, АР-40); после выхода — пустая лента и 404.
Лента — от новых к старым, курсор непрозрачный (позиция + HMAC, привязан к владельцу; чужой или
испорченный — 422). Автору — его оригинал (версию для получателя автор не видит, АР-25 п.3),
получателю — версия для получателя с пометкой `is_adapted`; недоставленное получателю не видно.
Автору у остановленного — причина `withheld_reason` и тип кризиса `withheld_crisis` (какой
протокол помощи показать, ТЗ §9.3; АР-44, АР-45); получателю и в других статусах — null.
Нерасшифрованный текст не скрывается: в `text` — `UnavailableText` с кодом причины (контракт —
`ExportText`, АР-40); в журнал — код причины и колонка.

Оригинал — получателю доставленного (просмотр фиксируется) и автору; чужое, недоставленное и
бывшей пары — 404, неразличимо; не расшифровался — 200 с `UnavailableText`, отметка просмотра
откатывается (засчитывается только показанный текст, АР-40).
«Прочитано» — получатель доставленного; повтор и уже отвеченное — 204 без изменений.
40P01/40001 → 409 `conflict_retry` (общий обработчик, АР-24). Ответы — `no-store`."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel

from app.api.deps import AppSettings, Keys, OnboardedUser, SessionMaker
from app.errors import AppError, not_found
from app.repositories import couple_messages as repo
from app.repositories.couple_messages import FeedPosition, Gone
from app.schemas import ExportText, UnavailableText

router = APIRouter(tags=["messages"])
logger = logging.getLogger(__name__)

CURSOR_DOMAIN = b"feelings/couple-feed-cursor/v1\x00"


WithheldReason = Literal[
    "safety", "model_refusal", "pipeline_error", "attempts_exhausted", "couple_ended"
]
WithheldCrisis = Literal["violence", "threat_outgoing", "self_harm", "life_threat"]


class PartnerMessage(BaseModel):
    id: uuid.UUID
    direction: Literal["outgoing", "incoming"]
    text: ExportText
    is_adapted: bool
    harsh_tone_warning: bool
    status: Literal["processing", "withheld", "delivered", "read", "answered"]
    # Только автору и только при ``withheld`` (АР-44, АР-45); получателю и в остальных
    # статусах — null.
    withheld_reason: WithheldReason | None = None
    withheld_crisis: WithheldCrisis | None = None
    reply_to_id: uuid.UUID | None
    sent_at: datetime
    read_at: datetime | None


class FeedPage(BaseModel):
    items: list[PartnerMessage]
    next_cursor: str | None


class OriginalText(BaseModel):
    text: ExportText


# --------------------------------------------------------------------------- курсор


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sig(secret: str, user_id: uuid.UUID, payload: bytes) -> bytes:
    mac = hmac.new(secret.encode(), CURSOR_DOMAIN + user_id.bytes + payload, hashlib.sha256)
    return mac.digest()[:18]


def encode_cursor(secret: str, user_id: uuid.UUID, pos: FeedPosition) -> str:
    payload = f"{pos.sent_at.isoformat()}|{pos.id}".encode()
    return f"{_b64(payload)}.{_b64(_sig(secret, user_id, payload))}"


def decode_cursor(secret: str, user_id: uuid.UUID, cursor: str) -> FeedPosition:
    bad = AppError(422, "validation_error", "Проверьте данные")
    try:
        head, sig = cursor.split(".")
        payload = _unb64(head)
        if not hmac.compare_digest(_unb64(sig), _sig(secret, user_id, payload)):
            raise bad
        at, row_id = payload.decode().split("|")
        sent_at = datetime.fromisoformat(at)
        if sent_at.tzinfo is None:
            raise bad
        return FeedPosition(sent_at, uuid.UUID(row_id))
    except AppError:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        raise bad from exc


def _message_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise not_found() from exc


# --------------------------------------------------------------------------- пути


@router.get("/couple/messages")
async def feed(
    user_id: OnboardedUser,
    sm: SessionMaker,
    keys: Keys,
    settings: AppSettings,
    response: Response,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> FeedPage:
    secret = settings.session_secret.get_secret_value()
    before = decode_cursor(secret, user_id, cursor) if cursor is not None else None
    rows, after = await repo.read_page(sm, keys, user_id, limit=limit, before=before)
    response.headers["Cache-Control"] = "no-store"
    return FeedPage(
        items=[
            PartnerMessage(
                id=r.id,
                direction="outgoing" if r.outgoing else "incoming",
                text=r.text if r.text is not None else UnavailableText(reason=r.unavailable),
                is_adapted=r.is_adapted,
                harsh_tone_warning=r.harsh_tone_warning,
                status=r.status,
                reply_to_id=r.reply_to_id,
                sent_at=r.sent_at,
                read_at=r.read_at,
                withheld_reason=r.withheld_reason,
                withheld_crisis=r.withheld_crisis,
            )
            for r in rows
        ],
        next_cursor=encode_cursor(secret, user_id, after) if after is not None else None,
    )


@router.get("/couple/messages/{message_id}/original")
async def original(
    message_id: str, user_id: OnboardedUser, sm: SessionMaker, keys: Keys, response: Response
) -> OriginalText:
    msg = _message_id(message_id)
    try:
        body, reason = await repo.read_original(sm, keys, user_id, msg)
    except Gone as exc:
        raise not_found() from exc
    response.headers["Cache-Control"] = "no-store"
    return OriginalText(text=body if body is not None else UnavailableText(reason=reason))


@router.post("/couple/messages/{message_id}/read", status_code=204)
async def mark_read(message_id: str, user_id: OnboardedUser, sm: SessionMaker) -> Response:
    msg = _message_id(message_id)
    try:
        await repo.mark_read(sm, user_id, msg)
    except Gone as exc:
        raise not_found() from exc
    return Response(status_code=204, headers={"Cache-Control": "no-store"})

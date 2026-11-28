"""Черновики сообщения партнёру — `/drafts*` (ФТ-07, ФТ-08, ФТ-17; ТЗ §2 п.1–3, §4.3, §6.2 п.1–2,
§9.3; задача 3.1, A2; АР-20, АР-24, АР-27, АР-40).

Генерация и «другой вариант» — функции A3 (`app.ai.drafts`: контекст 2.1, вторая линия
безопасности, `draft.v0`, проверка цитат); модели — из `ChatRuntime` (`app.state.chat`, как в чате;
не создались — 503 `llm_unavailable`). Правка, отказ и «Отправить» — слой доступа
`repositories.drafts`; партнёру сообщение уходит **только** по `POST /drafts/{id}/send`
(`app_send_draft`, ТЗ §2 п.2) — ни создание, ни правка, ни «другой вариант», ни отказ, ни ошибки
записи в `partner_messages` не делают.

**Кризис** (§9.3): черновика нет, ответ — протокол и контакты помощи (`DraftCrisis`, 200), событие
— в `safety_events` (только тип) фоновой задачей `ChatRuntime.record_safety_events`: уход клиента
её не отменяет, остановка сервиса её дожидается.

Сопоставление ошибок (АР-40): `DraftError` — `no_turns`/`bad_reply_target`/`quote`→`draft_quote`/
`same`→`same_variant` — 422, `not_found` — 404, `not_editable` — 409, `owner_gone` — 401;
`LLMRefusal` — 503 `llm_refusal`, прочие `LLMError` — 503 `llm_unavailable` (`message` —
`LLMError.user_message`); нет активной пары — 409 `couple_not_active` (создание — до вызова
модели; отправка — FA003). 402 до этапа 4 не отдаётся. Требуется онбординг (403). Ответы с
текстом — `no-store`. В журнал — только событие и код, без текстов и id."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.ai import drafts as ai_drafts
from app.ai.drafts import DraftCrisis, DraftError, DraftResult
from app.ai.llm import LLMError, LLMRefusal
from app.ai.models import AgentModels
from app.api.chat import SAFETY_EVENTS_WAIT_S, runtime
from app.api.couple_messages import PartnerMessage
from app.api.deps import AppSettings, Keys, OnboardedUser, SessionMaker, unauthorized
from app.errors import AppError, not_found
from app.help import help_contacts
from app.repositories import drafts as repo
from app.repositories.drafts import DraftRow
from app.schemas import UnavailableText

router = APIRouter(tags=["drafts"])
logger = logging.getLogger(__name__)

NO_STORE = {"Cache-Control": "no-store"}

_DRAFT_ERRORS: dict[str, tuple[int, str, str]] = {
    "no_turns": (422, "no_turns", "Сначала расскажите агенту, что хотите передать"),
    "bad_reply_target": (422, "bad_reply_target", "Ответить можно только на полученное сообщение"),
    "quote": (422, "draft_quote", "Черновик не получился — попробуйте ещё раз"),
    "same": (422, "same_variant", "Другой вариант не получился — попробуйте уточнить пожелание"),
    "not_found": (404, "not_found", "Не найдено"),
    "not_editable": (409, "not_editable", "Этот черновик уже нельзя изменить"),
}


def _not_editable() -> AppError:
    return AppError(409, "not_editable", "Этот черновик уже нельзя изменить")


def _version_conflict() -> AppError:
    return AppError(409, "version_conflict", "Черновик изменился — проверьте текст ещё раз")


def _couple_not_active() -> AppError:
    return AppError(409, "couple_not_active", "Пара не активна — отправить сообщение некому")


# --------------------------------------------------------------------------- схемы


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply_to_id: uuid.UUID | None = None


class EditBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(min_length=1, max_length=4000)
    expected_version: int = Field(ge=1)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("пустой текст")
        return value


class RegenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    wish: str | None = Field(default=None, max_length=500)


class SendBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_version: int = Field(ge=1)


class Draft(BaseModel):
    kind: Literal["draft"] = "draft"
    id: uuid.UUID
    text: str
    reply_to_id: uuid.UUID | None
    version: int
    status: Literal["draft", "approved", "rejected", "blocked", "sent"]


class DraftCrisisOut(BaseModel):
    kind: Literal["crisis"] = "crisis"
    text: str
    draft_id: uuid.UUID | None
    contacts: list[dict[str, Any]]


def _draft_out(row: DraftRow) -> Draft:
    if row.text is None:
        # `Draft.text` в контракте — только строка (карточка A1, отчёт 3.1): нерасшифрованный
        # черновик не показывается; в журнал — только код причины.
        logger.warning("draft unavailable: reason=%s", row.unavailable)
        raise not_found()
    return Draft(
        id=row.id,
        text=row.text,
        reply_to_id=row.reply_to_id,
        version=row.version,
        status=row.status,
    )


def _from_result(r: DraftResult) -> Draft:
    return Draft(id=r.draft_id, text=r.text, reply_to_id=r.reply_to_id, version=r.version,
                 status="draft")  # fmt: skip


# --------------------------------------------------------------------------- общее


async def _models(request: Request, settings: AppSettings) -> AgentModels:
    try:
        return await runtime(request).agent_models(settings)
    except LLMError as exc:
        logger.warning("drafts: agent models unavailable: kind=%s", exc.kind)
        raise AppError(503, "llm_unavailable", exc.user_message) from exc


def _map_errors(exc: Exception) -> AppError:
    if isinstance(exc, DraftError):
        if exc.code == "owner_gone":
            return unauthorized()
        status, code, message = _DRAFT_ERRORS[exc.code]
        return AppError(status, code, message)
    if isinstance(exc, LLMRefusal):
        return AppError(503, "llm_refusal", exc.user_message)
    if isinstance(exc, LLMError):
        return AppError(503, "llm_unavailable", exc.user_message)
    raise exc


async def _crisis(
    request: Request, sm: Any, user_id: uuid.UUID, found: DraftCrisis, draft_id: uuid.UUID | None
) -> JSONResponse:
    """Протокол §9.3: событие — фоновой задачей (не теряется при уходе клиента), ответ ждёт её
    не дольше ``SAFETY_EVENTS_WAIT_S``; контакты — всегда."""
    task = runtime(request).record_safety_events(sm, user_id, found.safety_events)
    await asyncio.wait({task}, timeout=SAFETY_EVENTS_WAIT_S)
    logger.info("drafts: event=crisis")
    out = DraftCrisisOut(text=found.response.text, draft_id=draft_id, contacts=help_contacts())
    return JSONResponse(out.model_dump(mode="json"), status_code=200, headers=NO_STORE)


# --------------------------------------------------------------------------- пути


@router.post("/drafts")
async def create_draft(
    request: Request,
    user_id: OnboardedUser,
    sm: SessionMaker,
    keys: Keys,
    settings: AppSettings,
    payload: Annotated[CreateBody | None, Body()] = None,
) -> Response:
    if not await repo.has_active_couple(sm, user_id):
        raise _couple_not_active()
    models = await _models(request, settings)
    rt = runtime(request)
    try:
        out = await ai_drafts.generate_draft(
            sm,
            keys,
            user_id,
            models,
            reply_to_id=payload.reply_to_id if payload else None,
            embedder=rt.embedder,
            knowledge=rt.knowledge,
            prompt_set=rt.prompt_set,
        )
    except (DraftError, LLMError) as exc:
        err = _map_errors(exc)
        logger.info("drafts: event=error code=%s", err.code)
        raise err from exc
    if isinstance(out, DraftCrisis):
        return await _crisis(request, sm, user_id, out, None)
    logger.info("drafts: event=created")
    return JSONResponse(
        _from_result(out).model_dump(mode="json"), status_code=201, headers=NO_STORE
    )


@router.get("/drafts/{draft_id}")
async def get_draft(
    draft_id: str, user_id: OnboardedUser, sm: SessionMaker, keys: Keys, response: Response
) -> Draft:
    try:
        row = await repo.get(sm, keys, user_id, _id(draft_id))
    except repo.DraftNotFound as exc:
        raise not_found() from exc
    response.headers.update(NO_STORE)
    return _draft_out(row)


@router.patch("/drafts/{draft_id}")
async def edit_draft(
    draft_id: str,
    payload: EditBody,
    user_id: OnboardedUser,
    sm: SessionMaker,
    keys: Keys,
    response: Response,
) -> Draft:
    try:
        row = await repo.edit(
            sm,
            keys,
            user_id,
            _id(draft_id),
            payload.text,
            expected_version=payload.expected_version,
        )
    except repo.DraftNotFound as exc:
        raise not_found() from exc
    except repo.NotEditable as exc:
        raise _not_editable() from exc
    except repo.VersionConflict as exc:
        raise _version_conflict() from exc
    response.headers.update(NO_STORE)
    return _draft_out(row)


@router.post("/drafts/{draft_id}/regenerate")
async def regenerate_draft(
    draft_id: str,
    request: Request,
    user_id: OnboardedUser,
    sm: SessionMaker,
    keys: Keys,
    settings: AppSettings,
    payload: Annotated[RegenerateBody | None, Body()] = None,
) -> Response:
    target = _id(draft_id)
    models = await _models(request, settings)
    rt = runtime(request)
    try:
        out = await ai_drafts.regenerate_draft(
            sm,
            keys,
            user_id,
            models,
            target,
            wish=payload.wish if payload else None,
            embedder=rt.embedder,
            knowledge=rt.knowledge,
            prompt_set=rt.prompt_set,
        )
    except (DraftError, LLMError) as exc:
        err = _map_errors(exc)
        logger.info("drafts: event=error code=%s", err.code)
        raise err from exc
    if isinstance(out, DraftCrisis):
        return await _crisis(request, sm, user_id, out, target)
    logger.info("drafts: event=regenerated")
    return JSONResponse(
        _from_result(out).model_dump(mode="json"), status_code=200, headers=NO_STORE
    )


@router.post("/drafts/{draft_id}/reject", status_code=204)
async def reject_draft(draft_id: str, user_id: OnboardedUser, sm: SessionMaker) -> Response:
    try:
        await repo.reject(sm, user_id, _id(draft_id))
    except repo.DraftNotFound as exc:
        raise not_found() from exc
    except repo.NotEditable as exc:
        raise _not_editable() from exc
    return Response(status_code=204, headers=NO_STORE)


@router.post("/drafts/{draft_id}/send", status_code=202)
async def send_draft(
    draft_id: str, payload: SendBody, user_id: OnboardedUser, sm: SessionMaker, keys: Keys
) -> Response:
    try:
        m = await repo.send(
            sm, keys, user_id, _id(draft_id), expected_version=payload.expected_version
        )
    except repo.DraftNotFound as exc:
        raise not_found() from exc
    except repo.NotEditable as exc:
        raise _not_editable() from exc
    except repo.VersionConflict as exc:
        raise _version_conflict() from exc
    except repo.Unreadable as exc:
        # 3.1 р1 (A2), ревью MINOR-2: отдельного кода в контракте нет (Б-79, A1) — not_editable.
        logger.warning("draft unavailable: reason=%s", exc.reason)
        raise AppError(
            409, "not_editable", "Текст черновика недоступен — попросите агента написать заново"
        ) from exc
    except repo.Blocked as exc:
        raise AppError(
            422, "blocked_by_safety", "Черновик остановлен проверкой безопасности"
        ) from exc
    except repo.CoupleNotActive as exc:
        raise _couple_not_active() from exc
    except repo.BadReplyTarget as exc:
        raise AppError(
            422, "bad_reply_target", "Ответить можно только на полученное сообщение"
        ) from exc
    except repo.OwnerGone as exc:
        raise unauthorized() from exc
    logger.info("drafts: event=sent")
    out = PartnerMessage(
        id=m.id,
        direction="outgoing",
        text=m.text if m.text is not None else UnavailableText(reason=m.unavailable),
        is_adapted=m.is_adapted,
        harsh_tone_warning=m.harsh_tone_warning,
        status=m.status,
        reply_to_id=m.reply_to_id,
        sent_at=m.sent_at,
        read_at=m.read_at,
        # 202 — сообщение только что создано в ``processing``: причины остановки ещё нет.
        withheld_reason=None,
        withheld_crisis=None,
        withheld_crisis_blocks=None,
    )
    return JSONResponse(out.model_dump(mode="json"), status_code=202, headers=NO_STORE)


def _id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise not_found() from exc

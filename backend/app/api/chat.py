"""Личный чат с агентом (ФТ-05; ТЗ §4.2, §6, §9.3, §10; задача 2.1, этап 2; АР-30, АР-32).

``POST /chat/messages`` — порядок контракта:

1. вход (401), онбординг (403), тело (422);
2. реплика владельца сохраняется шифротекстом **до** любого вызова модели — одной транзакцией
   (``chat.add_owner_turn``; 40P01/40001 → 409 ``conflict_retry``, АР-24);
3. сборка контекста §6.1 (``build_context``) и вторая линия безопасности
   ``screen(models, built)`` — сборка передаётся целиком, классификатор видит реплики владельца
   до усечения бюджетом (правила этапа 2);
4. дневной лимит (``app.core.limits``): исчерпан — 429 с ``Retry-After``; при кризисе реплика
   засчитывается, но 429 не отдаётся (ТЗ §9.3, §11 п.5); 402 до этапа 4 не отдаётся;
5. поток SSE: ``stream_reply(..., safety=sc)`` в ``contextlib.aclosing`` → события ``delta``*,
   затем ровно одно из ``done`` / ``crisis`` / ``error``. Ответ агента сохраняется до
   завершающего события; склейка ``delta`` — ровно сохранённый текст.

Отключение клиента (и на шагах 3–4, и во время потока) отменяет все вызовы модели и дожидается
их: шаги 3–4 идут задачей, которую снимает наблюдатель ``http.disconnect``; поток — задача-
производитель, которую отменяет ответ ``EventStreamResponse``, а ``aclosing`` закрывает
генератор ``stream_reply`` (его ``finally`` отменяет и дожидается вызовов модели).

Сжатие истории (``compress_history``) — фоновая задача после ``done``/``crisis``; её ошибки
пользователю не видны. В журналы — только имя завершающего события, код ошибки, длительности и
модели; ни текста реплики, ни кусков ответа, ни id (CLAUDE.md, ТЗ §7)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import Receive, Scope, Send

from app.ai.chat import ChatEvent, Crisis, Delta, Done, SafetyScreen, screen, stream_reply
from app.ai.context import BuiltContext, ContextBudget, build_context
from app.ai.dialog import OwnerContext, Turn
from app.ai.embeddings import Embedder
from app.ai.knowledge import KnowledgeBase
from app.ai.llm import LLMError
from app.ai.models import AgentModels, openrouter_models_async
from app.ai.prompts import V0, PromptSet, system_prompt
from app.ai.summary import CompressionPolicy, compress_history
from app.api.deps import AppSettings, Keys, OnboardedUser, SessionMaker, unauthorized
from app.config import Settings
from app.core.crypto import Keyring
from app.core.limits import DailyLimit, LimitDecision, MemoryDailyLimit, window
from app.errors import AppError
from app.help import help_contacts
from app.repositories import chat as chat_repo
from app.repositories.chat import FeedPosition, OwnerGone
from app.schemas import ChatMessage, ChatMessageRequest, ChatPage, UnavailableText

router = APIRouter(tags=["chat"])
logger = logging.getLogger(__name__)

SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
KEEPALIVE_LINE = ": keep-alive\n\n"
# Коды события ``error`` (``ChatErrorCode``); прочие виды ``LLMError`` и ошибки кода/БД —
# ``internal`` (АР-32 п.4).
ERROR_CODES = frozenset({"refusal", "unavailable", "quota", "bad_output"})
INTERNAL_MESSAGE = "Не получилось ответить — попробуйте ещё раз. Ваше сообщение сохранено."
RATE_LIMITED_MESSAGE = "На сегодня лимит сообщений агенту исчерпан — продолжим завтра"
CLIENT_CLOSED = 499  # клиент ушёл до ответа (как у nginx); тело никто не прочтёт
MIN_KEEP_TURNS = 6  # тест 2.1 О-5: после сжатия классификатор должен видеть 3 реплики владельца
MAX_COMPRESSION_CALLS = 10  # «пока more» — но не бесконечно
CURSOR_DOMAIN = b"feelings/chat-cursor/v1\x00"

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


# --------------------------------------------------------------------------- окружение чата


@dataclass
class ChatRuntime:
    """Модели, лимит и фоновые задачи чата (``app.state.chat``). Модели — из конфигурации
    (OpenRouter, имена — ``FEELINGS_LLM_*``; создаются при первом запросе и кешируются; ошибка
    не кешируется); в тестах — заглушки ``app.ai.stub``. Лимит — Redis (``lifespan``), без него —
    счётчик в памяти."""

    models: AgentModels | None = None
    limiter: DailyLimit = field(default_factory=MemoryDailyLimit)
    embedder: Embedder | None = None
    knowledge: KnowledgeBase | None = None
    budget: ContextBudget | None = None  # None — ``ContextBudget()`` по умолчанию
    compression: CompressionPolicy = field(default_factory=CompressionPolicy)
    prompt_set: PromptSet = V0
    background: set[asyncio.Task[None]] = field(default_factory=set)
    _models_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        if self.compression.keep_turns < MIN_KEEP_TURNS:
            raise ValueError(f"CompressionPolicy.keep_turns ≥ {MIN_KEEP_TURNS}")

    async def agent_models(self, settings: Settings) -> AgentModels:
        if self.models is not None:
            return self.models
        async with self._models_lock:
            if self.models is None:
                self.models = await openrouter_models_async(
                    settings.llm_writer_model, settings.llm_classifier_model
                )
        return self.models

    def spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    async def drain(self) -> None:
        """Дождаться фоновых задач (тесты)."""
        while self.background:
            await asyncio.gather(*list(self.background), return_exceptions=True)

    async def aclose(self) -> None:
        """Остановка сервиса: фоновые задачи снимаются и дожидаются."""
        await _cancel_and_wait(*self.background)


def runtime(request: Request) -> ChatRuntime:
    rt: ChatRuntime | None = getattr(request.app.state, "chat", None)
    if rt is None:
        raise RuntimeError("chat runtime is not configured")
    return rt


async def _cancel_and_wait(*tasks: asyncio.Task[Any] | None) -> None:
    """Отменить задачи и дождаться всех; отмена текущей задачи во время ожидания не теряется."""
    pending = {t for t in tasks if t is not None and not t.done()}
    for task in pending:
        task.cancel()
    interrupted = False
    while pending:
        try:
            _, pending = await asyncio.wait(pending)
        except asyncio.CancelledError:
            interrupted = True
    for t in tasks:
        if t is not None and t.done() and not t.cancelled():
            t.exception()  # забрать исключение — оно уже не нужно
    if interrupted:
        raise asyncio.CancelledError


# --------------------------------------------------------------------------- SSE


def sse(event: str, data: dict[str, Any]) -> str:
    """Событие SSE: ``event:``, ``data:`` — JSON в одну строку (переводы строк экранирует JSON)."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


async def _wait_disconnect(receive: Receive) -> None:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return


class ClientGone(Exception):
    pass


async def until_disconnect[T](receive: Receive, coro: Coroutine[Any, Any, T]) -> T:
    """Выполнить ``coro`` задачей; клиент ушёл раньше — задача отменяется и дожидается,
    бросается ``ClientGone``. Тело запроса к этому моменту уже прочитано."""
    work = asyncio.create_task(coro)
    gone = asyncio.create_task(_wait_disconnect(receive))
    try:
        await asyncio.wait({work, gone}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not work.done():
            await _cancel_and_wait(work, gone)
        else:
            await _cancel_and_wait(gone)
    if work.cancelled():
        raise ClientGone
    return work.result()


class EventStreamResponse(StreamingResponse):
    """Поток SSE, который пишет задача-производитель ``producer(emit)``. Между событиями дольше
    ``keepalive_s`` — строка-комментарий. Клиент ушёл (``http.disconnect`` или ошибка записи) —
    производитель отменяется и дожидается (вызовы модели отменены)."""

    def __init__(self, producer: Callable[[Emit], Awaitable[None]], keepalive_s: float) -> None:
        super().__init__((), media_type="text/event-stream", headers=SSE_HEADERS)
        self._producer = producer
        self._keepalive_s = keepalive_s

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def emit(event: str, data: dict[str, Any]) -> None:
            await queue.put(sse(event, data))

        async def run() -> None:
            try:
                await self._producer(emit)
            finally:
                queue.put_nowait(None)

        producer = asyncio.create_task(run())
        gone = asyncio.create_task(_wait_disconnect(receive))
        getter: asyncio.Task[str | None] | None = None
        try:
            await send({"type": "http.response.start", "status": 200, "headers": self.raw_headers})
            while True:
                if getter is None:
                    getter = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {getter, gone}, timeout=self._keepalive_s, return_when=asyncio.FIRST_COMPLETED
                )
                if gone in done:
                    return
                if getter not in done:
                    await self._body(send, KEEPALIVE_LINE)
                    continue
                item = getter.result()
                getter = None
                if item is None:
                    break
                await self._body(send, item)
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except OSError:
            return  # ASGI 2.4: запись в закрытое соединение
        finally:
            await _cancel_and_wait(getter, gone, producer)

    @staticmethod
    async def _body(send: Send, chunk: str) -> None:
        await send({"type": "http.response.body", "body": chunk.encode(), "more_body": True})


# --------------------------------------------------------------------------- POST


@dataclass
class _Prepared:
    built: BuiltContext | None = None
    screen: SafetyScreen | None = None
    models: AgentModels | None = None
    models_error: LLMError | None = None
    screen_ms: int = 0


async def _prepare(
    rt: ChatRuntime,
    settings: Settings,
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    body: str,
) -> _Prepared:
    """Шаг 3: модели, сборка контекста §6.1 и классификатор. Ошибка сборки не лишает реплику
    второй линии: классификатор видит хотя бы её саму (кризисный ответ — всегда, §9.3)."""
    out = _Prepared()
    try:
        out.models = await rt.agent_models(settings)
    except LLMError as exc:
        out.models_error = exc
        logger.warning("chat: agent models unavailable: kind=%s", exc.kind)
        return out
    try:
        out.built = await build_context(
            sm,
            keys,
            user_id,
            conversation_id,
            query=body,
            embedder=rt.embedder,
            knowledge=rt.knowledge,
            budget=rt.budget,
            prompt_set=rt.prompt_set,
        )
    except OwnerGone as exc:
        raise unauthorized() from exc
    except Exception as exc:
        logger.warning("chat: context build failed: %s", type(exc).__name__)
    started = time.monotonic()
    try:
        # Сборка — целиком: ``screen`` берёт ``built.safety_dialog`` (до усечения бюджетом).
        source: BuiltContext | list[Turn] = (
            out.built if out.built is not None else [Turn("owner", body)]
        )
        out.screen = await screen(out.models, source, prompt_set=rt.prompt_set)
    except Exception as exc:
        # Ошибка кода классификатора ответ не блокирует (как ошибка модели второй линии).
        logger.warning("chat: safety screen failed: %s", type(exc).__name__)
        out.screen = SafetyScreen(None, "internal", system_prompt("safety", rt.prompt_set).name)
    out.screen_ms = round((time.monotonic() - started) * 1000)
    return out


async def _acquire(
    rt: ChatRuntime, settings: Settings, user_id: uuid.UUID, *, force: bool
) -> LimitDecision:
    try:
        return await rt.limiter.acquire(user_id, settings.chat_daily_limit, force=force)
    except Exception as exc:
        # Хранилище счётчика недоступно: ответ важнее учёта (лимит — защита расходов, §10).
        logger.warning("chat: daily limit store unavailable: %s", type(exc).__name__)
        return LimitDecision(True, 0, window(datetime.now(UTC))[1])


def _error_payload(code: str, message: str | None) -> dict[str, Any]:
    if code in ERROR_CODES and message:
        return {"code": code, "message": message}
    return {"code": "internal", "message": INTERNAL_MESSAGE}


@dataclass
class _Outcome:
    started: float
    event: str | None = None
    code: str | None = None
    first_delta_ms: int | None = None
    models: tuple[str, ...] = ()
    fallback: bool = False


@dataclass
class _Reply:
    """Одна реплика владельца и ответ на неё: всё, что нужно потоку и фоновому сжатию."""

    rt: ChatRuntime
    sm: async_sessionmaker[AsyncSession]
    keys: Keyring
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    body: str
    prep: _Prepared
    out: _Outcome

    async def produce(self, emit: Emit) -> None:
        """Производитель потока SSE (``EventStreamResponse``): ровно одно завершающее событие."""
        out = self.out
        try:
            await self._stream(emit)
        except asyncio.CancelledError:
            out.event = out.event or "cancelled"
            raise
        except Exception as exc:
            logger.warning("chat: stream failed: %s", type(exc).__name__)
            if out.event is None:
                await self._final(emit, "error", _error_payload("internal", None))
        finally:
            logger.info(
                "chat reply: event=%s code=%s screen_ms=%s first_delta_ms=%s total_ms=%s "
                "models=%s fallback=%s",
                out.event,
                out.code,
                self.prep.screen_ms,
                out.first_delta_ms,
                round((time.monotonic() - out.started) * 1000),
                ",".join(out.models) or "-",
                out.fallback,
            )

    async def _final(self, emit: Emit, event: str, data: dict[str, Any]) -> None:
        self.out.event = event
        self.out.code = data.get("code") if event == "error" else None
        await emit(event, data)

    async def _stream(self, emit: Emit) -> None:
        prep, out = self.prep, self.out
        if prep.models is None:
            exc = prep.models_error
            payload = _error_payload(exc.kind, exc.user_message) if exc else None
            await self._final(emit, "error", payload or _error_payload("internal", None))
            return
        crisis_first = prep.screen is not None and prep.screen.is_crisis
        if prep.built is not None:
            context: OwnerContext = prep.built.context
            dialog: Sequence[Turn] = prep.built.dialog
        elif crisis_first:
            # Контекст не собрался, но кризис найден по самой реплике: ответ протокола не
            # зависит от контекста и идёт всегда (§9.3).
            context, dialog = OwnerContext("", ""), [Turn("owner", self.body)]
        else:
            await self._final(emit, "error", _error_payload("internal", None))
            return

        streamed: list[str] = []
        # ``stream_reply`` — асинхронный генератор (объявлен как ``AsyncIterator``): ``aclosing``
        # закрывает его и при выходе клиента — ``finally`` генератора отменяет вызовы модели.
        gen = cast(
            AsyncGenerator[ChatEvent, None],
            stream_reply(
                prep.models, context, dialog, prompt_set=self.rt.prompt_set, safety=prep.screen
            ),
        )
        async with contextlib.aclosing(gen) as events:
            async for ev in events:
                if isinstance(ev, Delta):
                    if out.first_delta_ms is None:
                        out.first_delta_ms = round((time.monotonic() - out.started) * 1000)
                    streamed.append(ev.text)
                    await emit("delta", {"text": ev.text})
                    continue
                out.models = tuple(dict.fromkeys(r.model for r in ev.calls))
                out.fallback = any(r.fallback_used for r in ev.calls)
                if isinstance(ev, Done):
                    await self._on_done(emit, ev, streamed)
                elif isinstance(ev, Crisis):
                    await self._on_crisis(emit, ev)
                else:
                    await self._final(emit, "error", _error_payload(ev.code, ev.message))
                return

    async def _on_done(self, emit: Emit, ev: Done, streamed: list[str]) -> None:
        # Сохраняется ровно склейка показанных кусков (контракт: склейка ``delta`` —
        # сохранённый текст); ``Done.reply`` — она же без пробелов по краям.
        text = "".join(streamed)
        if not streamed:  # pragma: no cover — ответ непустой, куски шли (страховка инварианта)
            text = ev.reply
            await emit("delta", {"text": text})
        elif text.strip() != ev.reply.strip():
            logger.warning("chat: streamed text differs from the final reply")
            await self._final(emit, "error", _error_payload("internal", None))
            return
        message_id = await chat_repo.add_message(
            self.sm, self.keys, self.user_id, self.conversation_id, "agent", text
        )
        if message_id is None:  # владелец удалён во время ответа (Б-44)
            await self._final(emit, "error", _error_payload("internal", None))
            return
        self._compress_later()
        await self._final(
            emit, "done", {"message_id": str(message_id), "suggest_draft": ev.suggest_draft}
        )

    async def _on_crisis(self, emit: Emit, ev: Crisis) -> None:
        """Кризисный ответ с контактами идёт всегда: сбой записи не отменяет событие (§9.3)."""
        message_id: uuid.UUID | None = None
        try:
            message_id = await chat_repo.add_message(
                self.sm, self.keys, self.user_id, self.conversation_id, "agent", ev.text
            )
        except Exception as exc:
            logger.warning("chat: crisis reply not saved: %s", type(exc).__name__)
        try:
            await chat_repo.record_safety_events(self.sm, self.user_id, ev.safety_events)
        except Exception as exc:
            logger.warning("chat: safety events not recorded: %s", type(exc).__name__)
        if message_id is not None:
            self._compress_later()
        await self._final(
            emit,
            "crisis",
            {
                "message_id": str(message_id) if message_id is not None else None,
                "text": ev.text,
                "replace": ev.replaces_streamed,
                "contacts": help_contacts(),
            },
        )

    def _compress_later(self) -> None:
        if self.prep.models is not None:
            self.rt.spawn(self._compress(self.prep.models))

    async def _compress(self, models: AgentModels) -> None:
        """Сжатие истории в фоне: повтор, пока хвост длиннее порога (``more``); ошибка модели
        или кода пользователю не видна — сводка прежняя, повтор при следующей реплике."""
        status, rounds, error = "skipped", 0, None
        try:
            for _ in range(MAX_COMPRESSION_CALLS):
                res = await compress_history(
                    self.sm,
                    self.keys,
                    self.user_id,
                    self.conversation_id,
                    models,
                    policy=self.rt.compression,
                )
                status, error = res.status, res.error
                rounds += res.rounds
                if res.status != "saved" or not res.more:
                    break
        except Exception as exc:
            status, error = "failed", type(exc).__name__
        if status != "skipped":
            logger.info("chat compression: status=%s rounds=%s error=%s", status, rounds, error)


@router.post("/chat/messages")
async def post_message(
    request: Request,
    payload: ChatMessageRequest,
    user_id: OnboardedUser,
    sm: SessionMaker,
    keys: Keys,
    settings: AppSettings,
) -> Response:
    rt = runtime(request)
    started = time.monotonic()
    try:
        conversation_id, _ = await chat_repo.add_owner_turn(sm, keys, user_id, payload.text)
    except OwnerGone as exc:
        raise unauthorized() from exc
    try:
        prep = await until_disconnect(
            request.receive,
            _prepare(rt, settings, sm, keys, user_id, conversation_id, payload.text),
        )
    except ClientGone:
        logger.info("chat reply: event=client_gone stage=prepare")
        return Response(status_code=CLIENT_CLOSED)
    crisis = prep.screen is not None and prep.screen.is_crisis
    decision = await _acquire(rt, settings, user_id, force=crisis)
    if not decision.allowed:
        logger.info("chat reply: event=rate_limited")
        return JSONResponse(
            status_code=429,
            content={"code": "rate_limited", "message": RATE_LIMITED_MESSAGE},
            headers={"Retry-After": str(decision.retry_after_s), "Cache-Control": "no-store"},
        )
    reply = _Reply(rt, sm, keys, user_id, conversation_id, payload.text, prep, _Outcome(started))
    return EventStreamResponse(reply.produce, settings.chat_keepalive_s)


# --------------------------------------------------------------------------- GET (история)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _cursor_sig(secret: str, user_id: uuid.UUID, payload: bytes) -> bytes:
    mac = hmac.new(secret.encode(), CURSOR_DOMAIN + user_id.bytes + payload, hashlib.sha256)
    return mac.digest()[:18]


def encode_cursor(secret: str, user_id: uuid.UUID, pos: FeedPosition) -> str:
    """Непрозрачный курсор: позиция реплики + подпись, привязанная к владельцу (чужой или
    составленный клиентом курсор не разбирается — 422)."""
    payload = f"{pos.created_at.isoformat()}|{pos.id}".encode()
    return f"{_b64(payload)}.{_b64(_cursor_sig(secret, user_id, payload))}"


def decode_cursor(secret: str, user_id: uuid.UUID, cursor: str) -> FeedPosition:
    bad = AppError(422, "validation_error", "Проверьте данные")
    try:
        head, sig = cursor.split(".")
        payload = _unb64(head)
        if not hmac.compare_digest(_unb64(sig), _cursor_sig(secret, user_id, payload)):
            raise bad
        at, row_id = payload.decode().split("|")
        created_at = datetime.fromisoformat(at)
        if created_at.tzinfo is None:
            raise bad
        return FeedPosition(created_at, uuid.UUID(row_id))
    except AppError:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        raise bad from exc


@router.get("/chat/messages")
async def get_messages(
    user_id: OnboardedUser,
    sm: SessionMaker,
    keys: Keys,
    settings: AppSettings,
    response: Response,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ChatPage:
    secret = settings.session_secret.get_secret_value()
    before = decode_cursor(secret, user_id, cursor) if cursor is not None else None
    rows, more = await chat_repo.read_chat_page(sm, keys, user_id, limit=limit, before=before)
    items = [
        ChatMessage(
            id=r.id,
            author="owner" if r.author == "owner" else "agent",
            text=r.text if r.text is not None else UnavailableText(reason=r.unavailable),
            created_at=r.created_at,
        )
        for r in rows
    ]
    next_cursor = (
        encode_cursor(secret, user_id, FeedPosition(rows[-1].created_at, rows[-1].id))
        if more and rows
        else None
    )
    response.headers["Cache-Control"] = "no-store"
    return ChatPage(items=items, next_cursor=next_cursor)

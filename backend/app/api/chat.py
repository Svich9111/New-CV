"""Личный чат с агентом (ФТ-05; ТЗ §4.2, §6, §9.3, §10; задача 2.1, этап 2; АР-30, АР-32).

``POST /chat/messages`` — порядок контракта:

1. вход (401), онбординг (403), тело (422);
2. реплика владельца сохраняется шифротекстом **до** любого вызова модели — одной транзакцией
   (``chat.add_owner_turn``; 40P01/40001 → 409 ``conflict_retry``, АР-24); иной сбой записи —
   классификатор по самой реплике: кризис — ``crisis`` с ``message_id: null``, иначе 500;
3. сборка контекста §6.1 (``build_context``) и вторая линия безопасности
   ``screen(models, built)`` — сборка передаётся целиком, классификатор видит реплики владельца
   до усечения бюджетом (правила этапа 2);
4. дневной лимит (``app.core.limits``): исчерпан — 429 с ``Retry-After``; при кризисе и без
   вердикта второй линии реплика засчитывается, но 429 не отдаётся — ответит первая линия
   (ТЗ §9.3, §11 п.5; АР-34 п.1); 402 до этапа 4 не отдаётся. Без вердикта второй линии каждое
   ``error`` несёт указание на помощь (АР-34 п.2);
5. поток SSE: ``stream_reply(..., safety=sc)`` в ``contextlib.aclosing`` → события ``delta``*,
   затем ровно одно из ``done`` / ``crisis`` / ``error``. Ответ агента сохраняется до
   завершающего события; склейка ``delta`` — ровно сохранённый текст.

С ключом идемпотентности ``client_message_id`` (АР-51; ``_post_keyed``) шаг 2 —
``chat.accept_owner_turn``: повтор с тем же ключом новой реплики не создаёт (ответ сохранён — он
же потоком без модели и лимита; реплика без ответа — ответ на неё; ответ готовит другой запрос —
409 ``conflict_retry``; под ключом другой текст — 422), первое событие потока — ``accepted``.
Без ключа — всё как раньше (``accepted`` не отдаётся).

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
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.exc import DBAPIError
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
from app.core.limits import DailyLimit, LimitDecision, MemoryDailyLimit
from app.db import RETRYABLE_SQLSTATES, sqlstate
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
CONFLICT_MESSAGE = "Не получилось с первого раза — повторите, пожалуйста"
RATE_LIMITED_MESSAGE = "На сегодня лимит сообщений агенту исчерпан — продолжим завтра"
# Служебный код журнала доступа (как у nginx): клиент ушёл до начала ответа, тело никто не
# прочтёт. В контракте его нет — клиент его не видит (ревью 2.1 э.2 MINOR-7; объявление — A1).
CLIENT_CLOSED = 499
MIN_KEEP_TURNS = 6  # тест 2.1 О-5: после сжатия классификатор должен видеть 3 реплики владельца
MAX_COMPRESSION_CALLS = 10  # «пока more» — но не бесконечно
COMPRESSION_SLOTS = 2  # одновременных сжатий на процесс (разные диалоги)
LIMIT_STORE_TIMEOUT_S = 0.5  # Redis отвечает за миллисекунды; дольше — считаем недоступным
LIMIT_STORE_COOLDOWN_S = 30.0  # «выключатель»: после сбоя столько не ходим в Redis (АР-34)
SAFETY_EVENTS_TIMEOUT_S = 10.0  # запись safety_events в фоне — не дольше
SAFETY_EVENTS_WAIT_S = 1.0  # сколько кризисное событие ждёт этой записи (контакты не задерживать)
# Аренда ответа на реплику с ключом (АР-51): дольше любого ответа модели; сбойный процесс
# отпускает реплику по этому сроку, штатно аренда снимается по завершении запроса.
REPLY_LEASE_S = 300.0
LEASE_RELEASE_TIMEOUT_S = 10.0
BUSY_MESSAGE = "Ответ на это сообщение ещё готовится — повторите через несколько секунд"
CURSOR_DOMAIN = b"feelings/chat-cursor/v1\x00"

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


# --------------------------------------------------------------------------- окружение чата


@dataclass
class ChatRuntime:
    """Модели, лимит и фоновые задачи чата (``app.state.chat``). Модели — из конфигурации
    (OpenRouter, имена — ``FEELINGS_LLM_*``; создаются при первом запросе и кешируются; ошибка
    не кешируется); в тестах — заглушки ``app.ai.stub``. Лимит — Redis (``lifespan``), без него —
    счётчик в памяти. Хранилище лимита недоступно (ошибка или дольше ``limit_timeout_s``) —
    «выключатель» на ``limit_cooldown_s``: в это время Redis не опрашивается, считает запасной
    счётчик процесса ``limit_fallback`` (АР-34), в журнал — ``error``."""

    models: AgentModels | None = None
    limiter: DailyLimit = field(default_factory=MemoryDailyLimit)
    embedder: Embedder | None = None
    knowledge: KnowledgeBase | None = None
    budget: ContextBudget | None = None  # None — ``ContextBudget()`` по умолчанию
    compression: CompressionPolicy = field(default_factory=CompressionPolicy)
    prompt_set: PromptSet = V0
    limit_fallback: MemoryDailyLimit = field(default_factory=MemoryDailyLimit)
    limit_timeout_s: float = LIMIT_STORE_TIMEOUT_S
    limit_cooldown_s: float = LIMIT_STORE_COOLDOWN_S
    reply_lease_s: float = REPLY_LEASE_S
    background: set[asyncio.Task[Any]] = field(default_factory=set)
    safety_writes: set[asyncio.Task[None]] = field(default_factory=set)
    _models_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _limit_down_until: float | None = None
    _compressing: dict[uuid.UUID, asyncio.Task[None]] = field(default_factory=dict)
    _compress_again: set[uuid.UUID] = field(default_factory=set)
    _compress_slots: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(COMPRESSION_SLOTS)
    )

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

    def spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    async def acquire(self, user_id: uuid.UUID, limit: int, *, force: bool) -> LimitDecision:
        """Дневной лимит с «выключателем» (АР-34): сбой хранилища не задерживает каждый запрос
        и не снимает лимит — считает запасной счётчик процесса."""
        now = asyncio.get_running_loop().time()
        if self._limit_down_until is None or now >= self._limit_down_until:
            try:
                return await asyncio.wait_for(
                    self.limiter.acquire(user_id, limit, force=force), self.limit_timeout_s
                )
            except Exception as exc:
                self._limit_down_until = now + self.limit_cooldown_s
                logger.error(
                    "chat: daily limit store unavailable (%s); process counter for %ss",
                    type(exc).__name__,
                    self.limit_cooldown_s,
                )
        return await self.limit_fallback.acquire(user_id, limit, force=force)

    def compress(
        self, conversation_id: uuid.UUID, job: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        """Одна задача сжатия на диалог (ревью MINOR-5): идёт — отмечается повтор после неё
        (появились новые реплики), вторая параллельно не стартует; общий предел
        ``COMPRESSION_SLOTS`` на процесс."""
        running = self._compressing.get(conversation_id)
        if running is not None and not running.done():
            self._compress_again.add(conversation_id)
            return

        async def run() -> None:
            try:
                while True:
                    async with self._compress_slots:
                        await job()
                    if conversation_id not in self._compress_again:
                        break
                    self._compress_again.discard(conversation_id)
            finally:
                if self._compressing.get(conversation_id) is asyncio.current_task():
                    del self._compressing[conversation_id]
                self._compress_again.discard(conversation_id)

        self._compressing[conversation_id] = self.spawn(run())

    async def drain(self) -> None:
        """Дождаться фоновых задач (тесты). ``gather`` из уже завершённых задач завершается
        синхронно, не отдавая управление циклу, — а задача уходит из ``background`` обратным
        вызовом, который цикл ещё должен выполнить; ``sleep(0)`` даёт ему выполниться (иначе
        цикл ``while`` без единой уступки — зависание)."""
        while self.background:
            await asyncio.gather(*list(self.background), return_exceptions=True)
            await asyncio.sleep(0)

    def record_safety_events(
        self,
        sm: async_sessionmaker[AsyncSession],
        user_id: uuid.UUID,
        types: Sequence[str],
        turn_id: uuid.UUID | None = None,
    ) -> asyncio.Task[None]:
        """Запись ``safety_events`` (только типы) фоновой задачей: уход клиента её не отменяет,
        остановка сервиса её дожидается (§9.3 «событие фиксируется»). ``turn_id`` — реплика
        владельца: по уже отмеченной реплике события повторно не пишутся (0020, АР-51)."""

        async def write() -> None:
            try:
                await asyncio.wait_for(
                    chat_repo.record_safety_events(sm, user_id, types, turn_id=turn_id),
                    SAFETY_EVENTS_TIMEOUT_S,
                )
            except Exception as exc:
                logger.error("chat: safety events not recorded: %s", type(exc).__name__)

        task = self.spawn(write())
        self.safety_writes.add(task)
        task.add_done_callback(self.safety_writes.discard)
        return task

    def release_lease(
        self,
        sm: async_sessionmaker[AsyncSession],
        user_id: uuid.UUID,
        turn: chat_repo.OwnerTurn | None,
    ) -> asyncio.Task[None] | None:
        """Снять аренду ответа (АР-51) фоновой задачей: отмена запроса (уход клиента) её не
        отменяет; сбой — аренда истечёт по сроку, в журнал — только имя ошибки. Задачу можно
        дождаться (``_LeaseRelease.wait``) — тогда немедленный повтор не застанет аренду."""
        if turn is None or turn.lease is None:
            return None
        message_id, lease = turn.message_id, turn.lease

        async def release() -> None:
            try:
                await asyncio.wait_for(
                    chat_repo.release_reply_lease(sm, user_id, message_id, lease),
                    LEASE_RELEASE_TIMEOUT_S,
                )
            except Exception as exc:
                logger.warning("chat: reply lease not released: %s", type(exc).__name__)

        return self.spawn(release())

    async def aclose(self) -> None:
        """Остановка сервиса: записи ``safety_events`` дожидаются (не дольше
        ``SAFETY_EVENTS_TIMEOUT_S``; A8-E3), остальные фоновые задачи (сжатие) снимаются."""
        others = [t for t in self.background if t not in self.safety_writes]
        writes = set(self.safety_writes)
        await _cancel_and_wait(*others)
        if writes:
            _, late = await asyncio.wait(writes, timeout=SAFETY_EVENTS_TIMEOUT_S)
            await _cancel_and_wait(*late)


@dataclass
class _LeaseRelease:
    """Снятие аренды ответа одной задачей на запрос (АР-51; ревью 2.2 р.2 MINOR-N2, A8 О-1).

    ``start`` — запустить (повторно — та же задача), синхронно, из любого места, в том числе из
    ``finally`` ответа после отмены производителя; ``wait`` — запустить и дождаться (не дольше
    ``LEASE_RELEASE_TIMEOUT_S``), не отменяя задачу при отмене ожидающего: так аренда снята до
    конца потока и до ответа 429, и немедленный повтор не получает 409."""

    rt: ChatRuntime
    sm: async_sessionmaker[AsyncSession]
    user_id: uuid.UUID
    turn: chat_repo.OwnerTurn | None
    _task: asyncio.Task[None] | None = None
    _started: bool = False

    def start(self) -> asyncio.Task[None] | None:
        if not self._started:
            self._started = True
            self._task = self.rt.release_lease(self.sm, self.user_id, self.turn)
        return self._task

    async def wait(self) -> None:
        task = self.start()
        if task is not None:
            await asyncio.wait({task}, timeout=LEASE_RELEASE_TIMEOUT_S)


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
    производитель отменяется и дожидается (вызовы модели отменены).

    ``before_end`` — дождаться после производителя, до закрытия потока (снятие аренды ответа:
    повтор сразу после завершающего события её не застаёт, A8 О-1); ``on_close`` — вызывается
    всегда при выходе из ответа, в том числе если производитель отменён, не успев начаться
    (ошибка записи заголовков; ревью 2.2 р.2 MINOR-N2)."""

    def __init__(
        self,
        producer: Callable[[Emit], Awaitable[None]],
        keepalive_s: float,
        *,
        before_end: Callable[[], Awaitable[None]] | None = None,
        on_close: Callable[[], object] | None = None,
    ) -> None:
        super().__init__((), media_type="text/event-stream", headers=SSE_HEADERS)
        self._producer = producer
        self._keepalive_s = keepalive_s
        self._before_end = before_end
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def emit(event: str, data: dict[str, Any]) -> None:
            await queue.put(sse(event, data))

        async def run() -> None:
            try:
                await self._producer(emit)
                if self._before_end is not None:
                    await self._before_end()
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
            try:
                await _cancel_and_wait(getter, gone, producer)
            finally:
                if self._on_close is not None:
                    self._on_close()

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
    # Запись ``safety_events`` по кризису классификатора — запущена сразу после вердикта, до
    # потока (ревью р.2 MINOR-3); ``None`` — кризиса до потока не было.
    safety_write: asyncio.Task[None] | None = None

    @property
    def crisis(self) -> bool:
        return self.screen is not None and self.screen.is_crisis

    @property
    def no_verdict(self) -> bool:
        """Вторая линия вердикта не дала (ошибка, отказ, недоступность, модели не созданы)."""
        return self.screen is None or self.screen.verdict is None


async def _prepare(
    rt: ChatRuntime,
    settings: Settings,
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    body: str,
    upto: FeedPosition | None = None,
) -> _Prepared:
    """Шаг 3: модели, сборка контекста §6.1 и классификатор. Ошибка сборки не лишает реплику
    второй линии: классификатор видит хотя бы её саму (кризисный ответ — всегда, §9.3).
    ``upto`` — ответ на реплику, сохранённую раньше (повтор с ключом): контекст — до неё."""
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
            upto=upto,
        )
    except OwnerGone as exc:
        raise unauthorized() from exc
    except Exception as exc:
        logger.warning("chat: context build failed: %s", type(exc).__name__)
    # Сборка — целиком: ``screen`` берёт ``built.safety_dialog`` (до усечения бюджетом).
    source: BuiltContext | list[Turn] = (
        out.built if out.built is not None else [Turn("owner", body)]
    )
    await _screen(rt, out.models, out, source)
    return out


async def _prepare_unsaved(rt: ChatRuntime, settings: Settings, body: str) -> _Prepared:
    """Реплику владельца сохранить не удалось (ревью MINOR-1): классификатор — по самой реплике,
    чтобы кризисный ответ ушёл и в этом случае (§9.3)."""
    out = _Prepared()
    try:
        out.models = await rt.agent_models(settings)
    except LLMError as exc:
        out.models_error = exc
        return out
    await _screen(rt, out.models, out, [Turn("owner", body)])
    return out


async def _screen(
    rt: ChatRuntime, models: AgentModels, out: _Prepared, source: BuiltContext | list[Turn]
) -> None:
    started = time.monotonic()
    try:
        out.screen = await screen(models, source, prompt_set=rt.prompt_set)
    except Exception as exc:
        # Ошибка кода классификатора ответ не блокирует (как ошибка модели второй линии), но
        # это дефект — в журнал уровнем error (ревью MAJOR-1).
        logger.error("chat: safety screen failed: %s", type(exc).__name__)
        out.screen = SafetyScreen(None, "internal", system_prompt("safety", rt.prompt_set).name)
    out.screen_ms = round((time.monotonic() - started) * 1000)


def help_hint() -> str:
    """Указание на помощь для ``error`` без вердикта второй линии (АР-34 п.2): номера — из того
    же списка, что ``GET /help/contacts`` (до проверки оператором — только 112)."""
    phones = ", ".join(c["phone"] for c in help_contacts())
    return (
        f"Если вам сейчас тяжело или небезопасно — звоните {phones}; контакты служб помощи — "
        "в разделе «Помощь»."
    )


def _error_payload(code: str, message: str | None, *, no_verdict: bool = False) -> dict[str, Any]:
    """Тело события ``error``. Без вердикта классификатора (кризис не исключён) — с указанием на
    помощь при любом коде (АР-34 п.2, ТЗ §9.3 «всегда», §2 п.7)."""
    if code in ERROR_CODES and message:
        payload = {"code": code, "message": message}
    else:
        payload = {"code": "internal", "message": INTERNAL_MESSAGE}
    if no_verdict:
        payload["message"] = f"{payload['message']} {help_hint()}"
    return payload


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
    conversation_id: uuid.UUID | None  # None — реплику владельца сохранить не удалось
    body: str
    prep: _Prepared
    out: _Outcome
    # Реплика владельца, на которую отвечаем (``reply_to`` ответа); None — не сохранена.
    turn_id: uuid.UUID | None = None
    # Тело события ``accepted`` — только при ключе ``client_message_id`` (АР-51).
    accepted: dict[str, Any] | None = None

    def _err(self, code: str = "internal", message: str | None = None) -> dict[str, Any]:
        return _error_payload(code, message, no_verdict=self.prep.no_verdict)

    async def produce(self, emit: Emit) -> None:
        """Производитель потока SSE (``EventStreamResponse``): ровно одно завершающее событие."""
        out = self.out
        try:
            if self.accepted is not None:
                await emit("accepted", self.accepted)
            await self._stream(emit)
        except asyncio.CancelledError:
            out.event = out.event or "cancelled"
            raise
        except Exception as exc:
            logger.warning("chat: stream failed: %s", type(exc).__name__)
            if out.event is None:
                await self._final(emit, "error", self._err())
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
            payload = self._err(exc.kind, exc.user_message) if exc else self._err()
            await self._final(emit, "error", payload)
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
            await self._final(emit, "error", self._err())
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
                    await self._final(emit, "error", self._err(ev.code, ev.message))
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
            await self._final(emit, "error", self._err())
            return
        if self.conversation_id is None:  # pragma: no cover — без диалога поток только кризисный
            await self._final(emit, "error", self._err())
            return
        message_id = await chat_repo.add_message(
            self.sm,
            self.keys,
            self.user_id,
            self.conversation_id,
            "agent",
            text,
            reply_to=self.turn_id,
        )
        if message_id is None:  # владелец удалён во время ответа (Б-44)
            await self._final(emit, "error", self._err())
            return
        self._compress_later()
        await self._final(
            emit, "done", {"message_id": str(message_id), "suggest_draft": ev.suggest_draft}
        )

    async def _on_crisis(self, emit: Emit, ev: Crisis) -> None:
        """Кризисный ответ с контактами идёт всегда: сбой записи не отменяет событие (§9.3).
        Запись ``safety_events`` — фоновая задача (ревью MINOR-3): уход клиента её не отменяет;
        событие ждёт её не дольше ``SAFETY_EVENTS_WAIT_S``, чтобы медленная БД не задерживала
        контакты."""
        events = self.prep.safety_write or self.rt.record_safety_events(
            self.sm, self.user_id, ev.safety_events, self.turn_id
        )
        message_id: uuid.UUID | None = None
        if self.conversation_id is not None:
            try:
                message_id = await chat_repo.add_message(
                    self.sm,
                    self.keys,
                    self.user_id,
                    self.conversation_id,
                    "agent",
                    ev.text,
                    reply_to=self.turn_id,
                    crisis=True,
                )
            except Exception as exc:
                logger.error("chat: crisis reply not saved: %s", type(exc).__name__)
        await asyncio.wait({events}, timeout=SAFETY_EVENTS_WAIT_S)  # не отменяет запись
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
        models, conversation_id = self.prep.models, self.conversation_id
        if models is not None and conversation_id is not None:
            self.rt.compress(conversation_id, lambda: self._compress(models, conversation_id))

    async def _compress(self, models: AgentModels, conversation_id: uuid.UUID) -> None:
        """Сжатие истории в фоне: повтор, пока хвост длиннее порога (``more``); ошибка модели
        или кода пользователю не видна — сводка прежняя, повтор при следующей реплике."""
        status, rounds, error = "skipped", 0, None
        try:
            for _ in range(MAX_COMPRESSION_CALLS):
                res = await compress_history(
                    self.sm,
                    self.keys,
                    self.user_id,
                    conversation_id,
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
    key = payload.client_message_id
    if key is not None:
        return await _post_keyed(request, rt, payload.text, key, user_id, sm, keys, settings)
    try:
        conversation_id, turn_id = await chat_repo.add_owner_turn(sm, keys, user_id, payload.text)
    except OwnerGone as exc:
        raise unauthorized() from exc
    except DBAPIError as exc:
        if sqlstate(exc) in RETRYABLE_SQLSTATES:
            raise  # 409 conflict_retry (АР-24): ничего не сохранено, повтор безопасен
        return await _unsaved_turn(request, rt, settings, sm, keys, user_id, payload.text, exc)
    except Exception as exc:
        return await _unsaved_turn(request, rt, settings, sm, keys, user_id, payload.text, exc)
    try:
        prep = await until_disconnect(
            request.receive,
            _prepare(rt, settings, sm, keys, user_id, conversation_id, payload.text),
        )
    except ClientGone:
        logger.info("chat reply: event=client_gone stage=prepare")
        return Response(status_code=CLIENT_CLOSED)
    _record_crisis_now(rt, sm, user_id, prep, turn_id)
    # АР-34 п.1: кризис или нет вердикта второй линии — 429 не отдаётся, реплика засчитывается.
    force = prep.crisis or prep.no_verdict
    decision = await rt.acquire(user_id, settings.chat_daily_limit, force=force)
    if not decision.allowed:
        return _rate_limited(decision)
    reply = _Reply(
        rt, sm, keys, user_id, conversation_id, payload.text, prep, _Outcome(started), turn_id
    )
    return EventStreamResponse(reply.produce, settings.chat_keepalive_s)


def _rate_limited(decision: LimitDecision) -> JSONResponse:
    logger.info("chat reply: event=rate_limited")
    return JSONResponse(
        status_code=429,
        content={"code": "rate_limited", "message": RATE_LIMITED_MESSAGE},
        headers={"Retry-After": str(decision.retry_after_s), "Cache-Control": "no-store"},
    )


def _accepted(message_id: uuid.UUID | None, key: uuid.UUID, *, replayed: bool) -> dict[str, Any]:
    return {
        "message_id": str(message_id) if message_id is not None else None,
        "client_message_id": str(key),
        "replayed": replayed,
    }


async def _post_keyed(
    request: Request,
    rt: ChatRuntime,
    body: str,
    key: uuid.UUID,
    user_id: uuid.UUID,
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    settings: Settings,
) -> Response:
    """Реплика с ключом идемпотентности ``client_message_id`` (АР-51). Повтор с тем же ключом
    новой реплики не создаёт: ответ сохранён — он же потоком (``_replay``); реплика без ответа —
    ответ на неё (контекст — до неё); ответ готовит другой запрос — 409 ``conflict_retry``;
    под ключом другой текст — 422. Дальше — тот же порядок, что без ключа; лимит за реплику
    засчитывается один раз (``limit_charged``); событие безопасности по реплике — тоже один раз
    (``safety_recorded``, 0020). Аренда ответа снимается на любом исходе — до ответа 429 и до
    конца потока (``_LeaseRelease``)."""
    started = time.monotonic()
    try:
        turn = await chat_repo.accept_owner_turn(
            sm, keys, user_id, body, key, lease_s=rt.reply_lease_s
        )
    except OwnerGone as exc:
        raise unauthorized() from exc
    except DBAPIError as exc:
        # 23505 — одновременная вставка того же ключа (страховка индекса 0018): как 40P01.
        if sqlstate(exc) in RETRYABLE_SQLSTATES or sqlstate(exc) == "23505":
            return _conflict(CONFLICT_MESSAGE)
        return await _unsaved_turn(request, rt, settings, sm, keys, user_id, body, exc, key=key)
    except Exception as exc:
        return await _unsaved_turn(request, rt, settings, sm, keys, user_id, body, exc, key=key)
    if turn.state == "busy":
        logger.info("chat reply: event=busy")
        return _conflict(BUSY_MESSAGE)
    if turn.state == "mismatch":
        raise AppError(422, "validation_error", "Проверьте данные")
    if turn.state == "answered":
        logger.info("chat reply: event=replayed")
        return EventStreamResponse(lambda emit: _replay(emit, turn, key), settings.chat_keepalive_s)
    replayed = turn.state == "unanswered"
    text = turn.text if turn.text is not None else body
    lease = _LeaseRelease(rt, sm, user_id, turn)
    handed = False
    try:
        try:
            prep = await until_disconnect(
                request.receive,
                _prepare(
                    rt,
                    settings,
                    sm,
                    keys,
                    user_id,
                    turn.conversation_id,
                    text,
                    turn.position if replayed else None,
                ),
            )
        except ClientGone:
            logger.info("chat reply: event=client_gone stage=prepare")
            return Response(status_code=CLIENT_CLOSED)
        _record_crisis_now(rt, sm, user_id, prep, turn.message_id)
        if not turn.limit_charged:
            force = prep.crisis or prep.no_verdict  # АР-34 п.1
            decision = await rt.acquire(user_id, settings.chat_daily_limit, force=force)
            if not decision.allowed:
                return _rate_limited(decision)
            await _mark_charged(sm, user_id, turn.message_id)
        reply = _Reply(
            rt,
            sm,
            keys,
            user_id,
            turn.conversation_id,
            text,
            prep,
            _Outcome(started),
            turn.message_id,
            _accepted(turn.message_id, key, replayed=replayed),
        )
        handed = True
        return EventStreamResponse(
            reply.produce, settings.chat_keepalive_s, before_end=lease.wait, on_close=lease.start
        )
    finally:
        if not handed:
            await lease.wait()


async def _mark_charged(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID, message_id: uuid.UUID
) -> None:
    """Отметка «лимит засчитан» (АР-32, АР-51). Сбой отметки ответ не блокирует: повтор тогда
    засчитается ещё раз (в журнал — только имя ошибки)."""
    try:
        await chat_repo.mark_limit_charged(sm, user_id, message_id)
    except Exception as exc:
        logger.warning("chat: limit mark not saved: %s", type(exc).__name__)


async def _replay(emit: Emit, turn: chat_repo.OwnerTurn, key: uuid.UUID) -> None:
    """Повтор с ключом, ответ уже сохранён (АР-51): ``accepted`` (``replayed: true``), затем
    обычный ответ — ``delta`` с сохранённым текстом целиком и ``done`` (``suggest_draft`` не
    хранится — false) или, для ответа кризисного протокола, ``crisis`` с контактами. Модель не
    вызывается, лимит не расходуется."""
    reply = turn.reply
    await emit("accepted", _accepted(turn.message_id, key, replayed=True))
    if reply is None or reply.text is None:
        await emit("error", _error_payload("internal", None))
    elif reply.crisis:
        payload = {
            "message_id": str(reply.id),
            "text": reply.text,
            "replace": False,
            "contacts": help_contacts(),
        }
        await emit("crisis", payload)
    else:
        if reply.text:
            await emit("delta", {"text": reply.text})
        await emit("done", {"message_id": str(reply.id), "suggest_draft": False})


def _conflict(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"code": "conflict_retry", "message": message},
        headers={"Cache-Control": "no-store"},
    )


def _record_crisis_now(
    rt: ChatRuntime,
    sm: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    prep: _Prepared,
    turn_id: uuid.UUID | None = None,
) -> None:
    """Кризис второй линии — событие безопасности записывается сразу после вердикта, до потока:
    клиент, ушедший до первого байта, его не отменит (ревью р.2 MINOR-3). ``turn_id`` — реплика
    владельца: по ней события пишутся один раз (повтор с ключом, 0020)."""
    if prep.crisis and prep.screen is not None:
        prep.safety_write = rt.record_safety_events(sm, user_id, prep.screen.crisis_types, turn_id)


async def _unsaved_turn(
    request: Request,
    rt: ChatRuntime,
    settings: Settings,
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    body: str,
    error: Exception,
    *,
    key: uuid.UUID | None = None,
) -> Response:
    """Реплику владельца сохранить не удалось (не 409 и не «владельца нет»; ревью MINOR-1):
    классификатор — по самой реплике; кризис — поток из одного ``crisis`` с
    ``message_id: null`` (контакты — всегда, §9.3); иначе — исходная ошибка (500)."""
    started = time.monotonic()
    logger.error("chat: owner turn not saved: %s", type(error).__name__)
    try:
        prep = await until_disconnect(request.receive, _prepare_unsaved(rt, settings, body))
    except ClientGone:
        return Response(status_code=CLIENT_CLOSED)
    if not prep.crisis and not prep.no_verdict:
        raise error  # вердикт «не кризис» есть: 500 по схеме Error (app.errors)
    # Кризис — поток из одного ``crisis``; без вердикта — из одного ``error internal`` с
    # указанием на помощь (АР-34 п.2, A8-E2): ответ модели без сохранённой реплики не нужен.
    _record_crisis_now(rt, sm, user_id, prep)
    await rt.acquire(user_id, settings.chat_daily_limit, force=True)
    accepted = _accepted(None, key, replayed=False) if key is not None else None
    reply = _Reply(rt, sm, keys, user_id, None, body, prep, _Outcome(started), None, accepted)
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
            client_message_id=r.client_message_id if r.author == "owner" else None,
            reply_to=r.reply_to_id if r.author == "agent" else None,
            crisis=r.crisis and r.author == "agent",
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

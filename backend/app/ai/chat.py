"""Потоковый ответ агента в личном чате (ФТ-05; ТЗ §4.2, §9.3, §10; задача 2.1, этап 1).

Поверх ``dialog.py`` (тот же промпт ``dialog.vN``, та же схема ответа, те же правила кризиса) и
``LLMProvider.stream_text``. Функция-генератор ``stream_reply`` отдаёт события:

- ``Delta(text)`` — очередной кусок ответа агента (поле ``reply`` его JSON-ответа, разбирается
  по мере прихода — первый кусок не ждёт конца ответа);
- ``Done`` — ответ закончен: итоговый текст для сохранения, уровень §6.4, ``suggest_draft``;
- ``Crisis`` — кризисный протокол §9.3: итоговый текст (ответ агента, если кризис нашёл он, и
  фиксированные блоки ``crisis.vN``), типы событий для ``safety_events``; последнее событие;
- ``ChatError`` — отказ или недоступность модели: код и понятный текст (§10); последнее.

Вторая линия безопасности — классификатор ``safety`` по последним репликам владельца:

- **до вызова агента** — ``screen()``; так требует контракт (сначала классификатор, потом
  подписка и лимиты: при кризисе 402/429 не отдаются). Найден кризис — ответ только из блоков
  протокола, модель агента не вызывается (кризисный ответ не зависит от её доступности);
- **параллельно** (``stream_reply`` без ``screen``) — агент уже пишет, но куски придерживаются
  до вердикта классификатора не дольше ``gate_timeout_s``; если вердикт пришёл позже и нашёл
  кризис, поток прерывается событием ``Crisis`` с ``replaces_streamed = True`` (клиент заменяет
  показанный текст). ``Done`` не отдаётся, пока классификатор не ответил (не дольше
  ``final_timeout_s``).

Кризисный ответ идёт всегда: и при отказе, и при недоступности, и при испорченном ответе агента
(АР-20; как ``dialog.reply``). Отказ модели (``refusal``) на резервную не уходит — это делает
``FallbackProvider`` (резерв — только недоступность и только до первого куска). Ошибка второй
линии ответ не блокирует (первая линия — сам агент), её вид — в ``safety_error``.

Тексты в журналы и записи вызовов не попадают (CLAUDE.md). Реплику владельца сохраняет API
**до** вызова (текст не теряется при ошибке); ответ агента — после ``Done``/``Crisis``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.ai import crisis
from app.ai.calls import CallLog, CallRecord, as_json
from app.ai.dialog import (
    DIALOG_CRISIS_CATEGORIES,
    MAX_TOKENS_DIALOG,
    SAFETY_TURNS,
    STRATEGIES,
    OwnerContext,
    SafetyVerdict,
    Turn,
    _agent_answer,
    _check_dialog,
    classify,
    error_reply,
    ordered_payload,
)
from app.ai.llm import ChatMessage, LLMBadOutput, LLMError
from app.ai.llm.errors import USER_MESSAGE_UNAVAILABLE
from app.ai.models import AgentModels
from app.ai.prompts import V0, PromptSet, system_prompt

# Классификатор (Haiku) отвечает за 1–2 с; придержка не должна съесть бюджет «первый символ
# ≤ 5 с» (§10). После неё куски идут, а вердикт ждётся параллельно.
SAFETY_GATE_TIMEOUT_S = 3.0
SAFETY_FINAL_TIMEOUT_S = 15.0


# --------------------------------------------------------------------------- события


@dataclass(frozen=True)
class Delta:
    text: str
    kind: Literal["delta"] = "delta"


@dataclass(frozen=True)
class Done:
    reply: str  # итоговый текст ответа агента — его API сохраняет как реплику агента
    level: int
    strategy: str
    suggest_draft: bool  # агент готов предложить черновик (сам черновик — задача 3.1)
    calls: tuple[CallRecord, ...]
    prompts: tuple[str, ...]
    safety_error: str | None = None
    kind: Literal["done"] = "done"


@dataclass(frozen=True)
class Crisis:
    text: str  # итоговый текст ответа (сохраняется как реплика агента)
    response: crisis.CrisisResponse
    replaces_streamed: bool  # показанные куски — не начало ``text``: клиент заменяет их
    safety_events: tuple[str, ...]  # crisis.CrisisType, главный — первым (без текста, §9.3)
    calls: tuple[CallRecord, ...]
    prompts: tuple[str, ...]
    safety_error: str | None = None
    level: int = 5
    strategy: str = STRATEGIES[5]
    kind: Literal["crisis"] = "crisis"


@dataclass(frozen=True)
class ChatError:
    code: str  # LLMError.kind (refusal, unavailable, quota, bad_output, …) или internal
    message: str  # понятный текст для человека; реплика владельца сохранена (§10)
    calls: tuple[CallRecord, ...]
    prompts: tuple[str, ...]
    safety_error: str | None = None
    kind: Literal["error"] = "error"


ChatEvent = Delta | Done | Crisis | ChatError


# --------------------------------------------------------------------------- вторая линия


@dataclass(frozen=True)
class SafetyScreen:
    """Вердикт классификатора по последним репликам владельца (или вид его ошибки)."""

    verdict: SafetyVerdict | None
    error: str | None
    prompt: str
    calls: tuple[CallRecord, ...] = field(default_factory=tuple)

    @property
    def crisis_types(self) -> tuple[str, ...]:
        """Строки §9.3 для личного чата: резкие слова о партнёре — не кризис (§2 п.4)."""
        if self.verdict is None:
            return ()
        return tuple(c for c in self.verdict.categories if c in DIALOG_CRISIS_CATEGORIES)

    @property
    def is_crisis(self) -> bool:
        return bool(self.crisis_types)


def _recent_owner_text(dialog: Sequence[Turn]) -> str:
    owner = [t.text for t in dialog if t.author == "owner"]
    return "\n".join(owner[-SAFETY_TURNS:])


async def screen(
    models: AgentModels, dialog: Sequence[Turn], *, prompt_set: PromptSet = V0
) -> SafetyScreen:
    """Классификатор безопасности по последним ``SAFETY_TURNS`` репликам владельца. Ошибка
    модели не бросается (``error`` — её вид): вторая линия не блокирует ответ. Прочие
    исключения — ошибки кода, бросаются."""
    _check_dialog(dialog)
    log = CallLog()
    name = system_prompt("safety", prompt_set).name
    try:
        verdict = await classify(models, "dialog", _recent_owner_text(dialog), log, prompt_set)
    except LLMError as exc:
        return SafetyScreen(None, exc.kind, name, tuple(log.records))
    return SafetyScreen(verdict, None, name, tuple(log.records))


# --------------------------------------------------------------------------- разбор потока


_REPLY_KEY = re.compile(r'(?<!\\)"reply"\s*:\s*"')
_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*(.*?)\s*```\s*$", re.DOTALL)
_SIMPLE_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r"}
_SIMPLE_ESCAPES["t"] = "\t"


class ReplyStream:
    """Достаёт значение строки ``reply`` из JSON-ответа по мере прихода кусков. Весь JSON
    проверяется в конце (``answer``) тем же разбором, что у ``dialog.reply``, и сверяется с
    показанным текстом."""

    def __init__(self) -> None:
        self._raw: list[str] = []
        self._buf = ""
        self._pos = 0
        self._state: Literal["seek", "string", "closed"] = "seek"
        self._decoded: list[str] = []

    @property
    def text(self) -> str:
        return "".join(self._decoded)

    @property
    def raw(self) -> str:
        return "".join(self._raw)

    def feed(self, chunk: str) -> str:
        """Новый кусок ответа модели → новый раскодированный текст ``reply`` (может быть "")."""
        self._raw.append(chunk)
        self._buf += chunk
        out: list[str] = []
        if self._state == "seek":
            match = _REPLY_KEY.search(self._buf)
            if match is None:
                return ""
            self._pos = match.end()
            self._state = "string"
        buf = self._buf
        i = self._pos
        while self._state == "string" and i < len(buf):
            ch = buf[i]
            if ch == '"':
                self._state = "closed"
                i += 1
                break
            if ch != "\\":
                out.append(ch)
                i += 1
                continue
            if i + 1 >= len(buf):
                break  # экранирование разрезано куском — ждём следующий
            esc = buf[i + 1]
            if esc in _SIMPLE_ESCAPES:
                out.append(_SIMPLE_ESCAPES[esc])
                i += 2
                continue
            if esc != "u":
                raise LLMBadOutput("неверное экранирование в ответе")
            if i + 6 > len(buf):
                break
            code = int(buf[i + 2 : i + 6], 16) if _is_hex(buf[i + 2 : i + 6]) else -1
            if code < 0:
                raise LLMBadOutput("неверное экранирование в ответе")
            if 0xD800 <= code <= 0xDBFF:  # суррогатная пара: нужна вторая половина
                if i + 12 > len(buf):
                    break
                low = buf[i + 6 : i + 12]
                if not (low.startswith("\\u") and _is_hex(low[2:])):
                    raise LLMBadOutput("неверная суррогатная пара в ответе")
                out.append(json.loads(f'"{buf[i : i + 12]}"'))
                i += 12
                continue
            out.append(chr(code))
            i += 6
        self._pos = i
        piece = "".join(out)
        self._decoded.append(piece)
        return piece

    def answer(self) -> Any:
        """Проверенный ответ агента (``dialog._AgentAnswer``); ``LLMBadOutput`` — JSON не
        разбирается, не по схеме или ``reply`` не совпал с показанным текстом."""
        raw = self.raw.strip()
        fenced = _FENCE.match(raw)
        if fenced:
            raw = fenced.group(1)
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end < start:
            raise LLMBadOutput("ответ агента — не JSON-объект")
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMBadOutput("ответ агента не разбирается как JSON") from exc
        if not isinstance(data, dict):
            raise LLMBadOutput("ответ агента — не JSON-объект")
        try:
            answer = _agent_answer(data)
        except (KeyError, TypeError, ValueError) as exc:
            raise LLMBadOutput(f"ответ агента не по схеме ({type(exc).__name__})") from exc
        if self._state != "closed" or answer.reply != self.text.strip():
            raise LLMBadOutput("показанный текст не совпал с ответом агента")
        return answer


def _is_hex(value: str) -> bool:
    return len(value) == 4 and all(c in "0123456789abcdefABCDEF" for c in value)


# --------------------------------------------------------------------------- поток ответа


def _crisis_event(
    types: Sequence[str],
    *,
    agent_reply: str | None,
    primary: str | None,
    streamed: str,
    prompt_set: PromptSet,
    log: CallLog,
    prompts: list[str],
    safety_error: str | None,
) -> Crisis:
    response = crisis.respond(
        types, agent_reply=agent_reply, primary=primary, prompt_set=prompt_set, withheld=False
    )
    prompts.append(f"crisis.{response.template_version}")
    shown = streamed.strip()
    return Crisis(
        text=response.text,
        response=response,
        replaces_streamed=bool(shown) and not response.text.startswith(shown),
        safety_events=response.types,
        calls=tuple(log.records),
        prompts=tuple(prompts),
        safety_error=safety_error,
    )


async def _cancel(task: asyncio.Task[Any] | None) -> None:
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(BaseException):
            await task


async def stream_reply(
    models: AgentModels,
    context: OwnerContext,
    dialog: Sequence[Turn],
    *,
    prompt_set: PromptSet = V0,
    safety: SafetyScreen | None = None,
    gate_timeout_s: float = SAFETY_GATE_TIMEOUT_S,
    final_timeout_s: float = SAFETY_FINAL_TIMEOUT_S,
) -> AsyncIterator[ChatEvent]:
    """Ответ агента потоком (см. описание модуля). ``safety`` — результат ``screen()``, если API
    уже проверил реплику (порядок контракта); без него классификатор идёт параллельно.

    Последнее событие — ``Done``, ``Crisis`` или ``ChatError``. Исключения, кроме ошибок модели,
    — ошибки кода: бросаются (API отвечает событием ``error`` с кодом ``internal``). Закрытие
    генератора (клиент ушёл) отменяет вызовы модели."""
    _check_dialog(dialog)
    log = CallLog()
    dialog_prompt = system_prompt("dialog", prompt_set)
    safety_name = system_prompt("safety", prompt_set).name
    prompts: list[str] = [dialog_prompt.name, safety_name]

    if safety is not None:
        log.records.extend(safety.calls)
        if safety.is_crisis:
            # Кризис найден до вызова агента: ответ только из блоков протокола (§9.3).
            yield _crisis_event(
                safety.crisis_types,
                agent_reply=None,
                primary=None,
                streamed="",
                prompt_set=prompt_set,
                log=log,
                prompts=prompts,
                safety_error=safety.error,
            )
            return

    provider = models.dialog
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def pump() -> None:
        started = time.monotonic()
        error: str | None = None
        try:
            async for chunk in provider.stream_text(
                system=dialog_prompt.text,
                messages=[ChatMessage("user", as_json(ordered_payload(context, dialog)))],
                max_tokens=MAX_TOKENS_DIALOG,
            ):
                await queue.put(("chunk", chunk))
            await queue.put(("end", None))
        except Exception as exc:  # передаётся в основной цикл; CancelledError — нет
            error = exc.kind if isinstance(exc, LLMError) else "internal"
            await queue.put(("exc", exc))
        finally:
            log.records.append(
                CallRecord(
                    role="dialog",
                    prompt=dialog_prompt.name,
                    model=provider.model,
                    latency_s=round(time.monotonic() - started, 3),
                    error=error,
                    usage_known=False,
                )
            )

    safety_task: asyncio.Task[SafetyScreen] | None = None
    if safety is None:
        safety_task = asyncio.create_task(screen(models, dialog, prompt_set=prompt_set))
    agent_task = asyncio.create_task(pump())
    get_task: asyncio.Task[tuple[str, Any]] | None = None
    parser = ReplyStream()
    pending: list[str] = []
    streamed: list[str] = []
    gate_open = safety is not None
    loop = asyncio.get_running_loop()
    gate_deadline = loop.time() + gate_timeout_s
    agent_exc: BaseException | None = None

    def take_safety() -> SafetyScreen | None:
        nonlocal safety, safety_task
        if safety_task is not None and safety_task.done():
            safety = safety_task.result()  # ошибка кода классификатора — бросается
            safety_task = None
            log.records.extend(safety.calls)
        return safety

    try:
        while True:
            sc = take_safety()
            if sc is not None:
                if sc.is_crisis:
                    yield _crisis_event(
                        sc.crisis_types,
                        agent_reply=None,
                        primary=None,
                        streamed="".join(streamed),
                        prompt_set=prompt_set,
                        log=log,
                        prompts=prompts,
                        safety_error=sc.error,
                    )
                    return
                gate_open = True
            if gate_open and pending:
                piece = "".join(pending)
                pending.clear()
                streamed.append(piece)
                yield Delta(piece)
            if get_task is None:
                get_task = asyncio.create_task(queue.get())
            waiters: set[asyncio.Task[Any]] = {get_task}
            timeout: float | None = None
            if safety_task is not None:
                waiters.add(safety_task)
                if not gate_open:
                    timeout = max(gate_deadline - loop.time(), 0.0)
            done, _ = await asyncio.wait(
                waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                gate_open = True  # классификатор медлит — куски идут, вердикт ждём параллельно
                continue
            if get_task not in done:
                continue  # пришёл вердикт — разбирается в начале цикла
            item_kind, value = get_task.result()
            get_task = None
            if item_kind == "chunk":
                try:
                    piece = parser.feed(value)
                except LLMBadOutput as exc:
                    agent_exc = exc
                    await _cancel(agent_task)
                    break
                if piece:
                    pending.append(piece)
            elif item_kind == "end":
                break
            else:
                agent_exc = value
                break

        # Агент закончил или упал: запись его вызова — в журнал; до итога нужен вердикт второй
        # линии.
        await asyncio.wait({agent_task})
        if safety_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(safety_task), final_timeout_s)
            except TimeoutError:
                await _cancel(safety_task)
                safety_task = None
                safety = SafetyScreen(None, "timeout", safety_name)
        sc = take_safety()
        safety_error = sc.error if sc is not None else None
        if sc is not None and sc.is_crisis:
            yield _crisis_event(
                sc.crisis_types,
                agent_reply=None,
                primary=None,
                streamed="".join(streamed),
                prompt_set=prompt_set,
                log=log,
                prompts=prompts,
                safety_error=safety_error,
            )
            return

        if agent_exc is None:
            try:
                answer = parser.answer()
            except LLMBadOutput as exc:
                agent_exc = exc
        if agent_exc is not None:
            if not isinstance(agent_exc, LLMError):
                raise agent_exc
            yield ChatError(
                code=agent_exc.kind,
                message=error_reply(agent_exc) or USER_MESSAGE_UNAVAILABLE,
                calls=tuple(log.records),
                prompts=tuple(prompts),
                safety_error=safety_error,
            )
            return

        if pending:
            piece = "".join(pending)
            pending.clear()
            streamed.append(piece)
            yield Delta(piece)
        if answer.crisis_type is not None:
            yield _crisis_event(
                (answer.crisis_type, *answer.crisis_secondary),
                agent_reply=answer.reply,
                primary=answer.crisis_type,
                streamed="".join(streamed),
                prompt_set=prompt_set,
                log=log,
                prompts=prompts,
                safety_error=safety_error,
            )
            return
        level = min(answer.level, 4)  # уровень 5 без строки протокола — не кризис
        yield Done(
            reply=answer.reply,
            level=level,
            strategy=STRATEGIES[level],
            suggest_draft=answer.draft_ready,
            calls=tuple(log.records),
            prompts=tuple(prompts),
            safety_error=safety_error,
        )
    finally:
        await _cancel(get_task)
        await _cancel(agent_task)
        await _cancel(safety_task)

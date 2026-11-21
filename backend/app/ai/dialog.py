"""Агент владельца: ответ в личном чате и черновик сообщения партнёру (задача 0.5; ФТ-05, ФТ-07).

Чистые функции над слоем модели, без БД. Контекст (ТЗ §6.1) передаётся аргументом
``OwnerContext`` и содержит **только** данные владельца и одобренные сообщения пары (§9.2 п.1):
полей для данных партнёра (его чат, память, агент) в нём нет. Сборку контекста из памяти и базы
знаний делают задачи 2.1, 2.4, 2.6.

Ответ (``reply``):
1. Параллельно: агент владельца (``dialog.v0``) и вторая линия — классификатор безопасности
   (``safety.v0``) по последним репликам владельца.
2. Кризис (§9.3), если его нашёл агент или классификатор: черновика нет, к ответу агента
   добавляются фиксированные блоки протокола (``crisis.v0``); если кризис нашёл только
   классификатор, ответ агента отбрасывается (он мог предложить «подобрать слова») и остаются
   только блоки. Уровень — 5, стратегия — ``crisis_protocol``.
3. Иначе — уровень и стратегия по шкале §6.4 (стратегию выводит код); если агент ответил
   ``draft_ready``, отдельный вызов ``draft.v0`` делает черновик «чувство — ситуация —
   потребность — просьба». Черновик не отправляется: автор правит его и подтверждает
   (``pipeline.approve``).

Ошибки — ``LLMError`` слоя (у каждой ``user_message``; черновик автора не теряется). Отказ модели
(АР-20) на резерв не уходит; текст для человека — ``error_reply``. Ошибка второй линии
безопасности ответ не блокирует (первая линия — сам агент); ошибка черновика оставляет ответ без
черновика (``draft_error``)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.ai import crisis
from app.ai.calls import (
    CallLog,
    CallRecord,
    complete,
    enum_list,
    int_field,
    nullable,
    strict_object,
    text_field,
)
from app.ai.llm import LLMError, LLMRefusal
from app.ai.models import AgentModels
from app.ai.prompts import V0, PromptSet, system_prompt

Author = Literal["owner", "agent"]
Direction = Literal["outgoing", "incoming"]

# Стратегия однозначно задаётся уровнем (ТЗ §6.4).
STRATEGIES: dict[int, str] = {
    1: "listen_and_request",
    2: "find_need_prepare_message",
    3: "deescalate_live_talk",
    4: "support_recommend_therapist",
    5: "crisis_protocol",
}
SAFETY_CATEGORIES = (
    "none",
    "threat_or_insult",
    "violence",
    "self_harm",
    "life_threat",
    "underage",
)
# В личном чате владелец вправе выговориться: резкие слова о партнёре — не кризис (§2 п.4).
DIALOG_CRISIS_CATEGORIES = ("violence", "self_harm", "life_threat", "underage")
SAFETY_TURNS = 3  # сколько последних реплик владельца видит вторая линия безопасности

MAX_TOKENS_DIALOG = 3000
MAX_TOKENS_DRAFT = 2000
MAX_TOKENS_CLASSIFIER = 1500

REFUSAL_REPLY = (
    "Я не смог ответить на это сообщение. Твой текст сохранён — можно написать иначе или "
    "вернуться позже. Если тебе сейчас тяжело или небезопасно, контакты служб помощи есть в "
    "разделе «Помощь»."
)


@dataclass(frozen=True)
class PairMessage:
    """Одобренное сообщение пары (§6.1 п.6): outgoing — от владельца, incoming — от партнёра."""

    direction: Direction
    text: str


@dataclass(frozen=True)
class OwnerContext:
    """Контекст агента владельца (§6.1 п.2–4, 6). Только данные владельца."""

    owner_name: str
    partner_name: str
    relationship: str = ""
    memory: tuple[str, ...] = ()
    knowledge: tuple[str, ...] = ()
    pair_messages: tuple[PairMessage, ...] = ()


@dataclass(frozen=True)
class Turn:
    """Реплика личного чата (§6.1 п.5): ``owner`` — владелец, ``agent`` — ответ агента."""

    author: Author
    text: str


@dataclass(frozen=True)
class Draft:
    """Черновик «чувство — ситуация — потребность — просьба» (§6.2 п.1). Не отправлен."""

    feeling: str
    situation: str
    need: str
    request: str
    text: str
    prompt: str


@dataclass(frozen=True)
class Revision:
    """Просьба автора о другом варианте черновика (ФТ-07)."""

    previous: str
    wish: str


@dataclass(frozen=True)
class DialogResult:
    level: int
    strategy: str
    reply: str
    draft: Draft | None
    crisis: crisis.CrisisResponse | None
    calls: tuple[CallRecord, ...]
    prompts: tuple[str, ...]
    draft_error: str | None = None  # вид ошибки, если черновик не получился
    safety_error: str | None = None  # вид ошибки второй линии безопасности

    @property
    def cost_usd(self) -> float:
        return round(sum(c.cost_usd for c in self.calls), 8)


@dataclass(frozen=True)
class _AgentAnswer:
    level: int
    reply: str
    draft_ready: bool
    crisis_type: str | None
    crisis_secondary: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SafetyVerdict:
    category: str
    secondary: tuple[str, ...]

    @property
    def categories(self) -> tuple[str, ...]:
        return tuple(c for c in (self.category, *self.secondary) if c != "none")


DIALOG_SCHEMA: dict[str, Any] = strict_object(
    {
        "level": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "reply": {"type": "string"},
        "draft_ready": {"type": "boolean"},
        "crisis_type": nullable({"type": "string", "enum": list(crisis.CRISIS_TYPES)}),
        "crisis_secondary": {
            "type": "array",
            "items": {"type": "string", "enum": list(crisis.CRISIS_TYPES)},
        },
    }
)
DRAFT_SCHEMA: dict[str, Any] = strict_object(
    {k: {"type": "string"} for k in ("feeling", "situation", "need", "request", "text")}
)
SAFETY_SCHEMA: dict[str, Any] = strict_object(
    {
        "category": {"type": "string", "enum": list(SAFETY_CATEGORIES)},
        "secondary": {
            "type": "array",
            "items": {"type": "string", "enum": list(SAFETY_CATEGORIES)},
        },
    }
)


# --------------------------------------------------------------------------- вход модели


def context_payload(context: OwnerContext, dialog: Sequence[Turn]) -> dict[str, Any]:
    """Вход агента владельца — ровно поля ``OwnerContext`` и диалог (ТЗ §6.1)."""
    return {
        "owner_name": context.owner_name,
        "partner_name": context.partner_name,
        "relationship": context.relationship,
        "owner_memory": list(context.memory),
        "knowledge": list(context.knowledge),
        "pair_messages": [
            {"direction": m.direction, "text": m.text} for m in context.pair_messages
        ],
        "dialog": [{"from": t.author, "text": t.text} for t in dialog],
    }


def safety_payload(kind: Literal["message", "dialog"], text: str) -> dict[str, Any]:
    return {"kind": kind, "text": text}


def _check_dialog(dialog: Sequence[Turn]) -> None:
    if not dialog or dialog[-1].author != "owner":
        raise ValueError("диалог должен заканчиваться репликой владельца")
    if any(t.author not in ("owner", "agent") or not t.text.strip() for t in dialog):
        raise ValueError("реплика без автора или пустая")


# --------------------------------------------------------------------------- проверка ответа


def _agent_answer(data: dict[str, Any]) -> _AgentAnswer:
    ctype = data["crisis_type"]
    if ctype is not None and ctype not in crisis.CRISIS_TYPES:
        raise ValueError("crisis_type")
    if not isinstance(data["draft_ready"], bool):
        raise TypeError("draft_ready")
    return _AgentAnswer(
        level=int_field(data, "level", range(1, 6)),
        reply=text_field(data, "reply"),
        draft_ready=data["draft_ready"],
        crisis_type=ctype,
        crisis_secondary=enum_list(data, "crisis_secondary", crisis.CRISIS_TYPES),
    )


def _draft(prompt: str) -> Any:
    def validate(data: dict[str, Any]) -> Draft:
        return Draft(
            feeling=text_field(data, "feeling"),
            situation=text_field(data, "situation"),
            need=text_field(data, "need"),
            request=text_field(data, "request"),  # схема §6.2 п.1 четырёхчастная
            text=text_field(data, "text"),
            prompt=prompt,
        )

    return validate


def safety_verdict(data: dict[str, Any]) -> SafetyVerdict:
    category = data["category"]
    if category not in SAFETY_CATEGORIES:
        raise ValueError("category")
    secondary = tuple(c for c in enum_list(data, "secondary", SAFETY_CATEGORIES) if c != category)
    return SafetyVerdict(category=category, secondary=secondary)


# --------------------------------------------------------------------------- вызовы


async def classify(
    models: AgentModels,
    kind: Literal["message", "dialog"],
    text: str,
    log: CallLog,
    prompt_set: PromptSet = V0,
) -> SafetyVerdict:
    """Классификатор безопасности (§6.2 п.3). Вход — только переданный текст."""
    return await complete(
        models.safety,
        prompt=system_prompt("safety", prompt_set),
        payload=safety_payload(kind, text),
        schema=SAFETY_SCHEMA,
        validate=safety_verdict,
        log=log,
        max_tokens=MAX_TOKENS_CLASSIFIER,
    )


async def draft(
    models: AgentModels,
    context: OwnerContext,
    dialog: Sequence[Turn],
    *,
    revision: Revision | None = None,
    prompt_set: PromptSet = V0,
    log: CallLog | None = None,
) -> Draft:
    """Черновик сообщения партнёру (ФТ-07); ``revision`` — другой вариант по просьбе автора.
    Не вызывать при кризисе — это делает ``reply``."""
    if revision is None:
        _check_dialog(dialog)
    prompt = system_prompt("draft", prompt_set)
    # Фрагменты базы знаний черновику не передаются: текст партнёру — слова владельца, книги в
    # нём не цитируются (§6.4; ревью 0.5 MINOR-5). Они нужны агенту в диалоге.
    payload = context_payload(context, dialog)
    payload.pop("knowledge")
    payload["revision"] = (
        {"previous": revision.previous, "wish": revision.wish} if revision is not None else None
    )
    return await complete(
        models.draft,
        prompt=prompt,
        payload=payload,
        schema=DRAFT_SCHEMA,
        validate=_draft(prompt.name),
        log=log if log is not None else CallLog(),
        max_tokens=MAX_TOKENS_DRAFT,
    )


async def reply(
    models: AgentModels,
    context: OwnerContext,
    dialog: Sequence[Turn],
    *,
    prompt_set: PromptSet = V0,
    with_draft: bool = True,
) -> DialogResult:
    """Ответ агента на последние реплики владельца (см. описание модуля)."""
    _check_dialog(dialog)
    log = CallLog()
    dialog_prompt = system_prompt("dialog", prompt_set)
    owner_text = "\n".join(t.text for t in dialog if t.author == "owner").strip()
    recent = "\n".join([t.text for t in dialog if t.author == "owner"][-SAFETY_TURNS:])
    agent_call = complete(
        models.dialog,
        prompt=dialog_prompt,
        payload=context_payload(context, dialog),
        schema=DIALOG_SCHEMA,
        validate=_agent_answer,
        log=log,
        max_tokens=MAX_TOKENS_DIALOG,
    )
    safety_call = classify(models, "dialog", recent or owner_text, log, prompt_set)
    answer_or_exc, verdict_or_exc = await asyncio.gather(
        agent_call, safety_call, return_exceptions=True
    )
    if isinstance(verdict_or_exc, BaseException) and not isinstance(verdict_or_exc, LLMError):
        raise verdict_or_exc
    verdict = verdict_or_exc if isinstance(verdict_or_exc, SafetyVerdict) else None
    safety_error = verdict_or_exc.kind if isinstance(verdict_or_exc, LLMError) else None
    classifier_types = tuple(
        c for c in (verdict.categories if verdict else ()) if c in DIALOG_CRISIS_CATEGORIES
    )
    prompts = [dialog_prompt.name, system_prompt("safety", prompt_set).name]
    if isinstance(answer_or_exc, BaseException):
        # Любая ошибка агента (отказ, недоступность после резерва, испорченный ответ) при
        # кризисе, найденном второй линией, — кризисный путь: шаблон от модели не зависит
        # (АР-20; ревью 0.5 MAJOR-3). Без кризиса — ошибка вызывающему.
        if not (isinstance(answer_or_exc, LLMError) and classifier_types):
            raise answer_or_exc
        answer = _AgentAnswer(level=5, reply="", draft_ready=False, crisis_type=None)
    else:
        answer = answer_or_exc

    agent_types = (
        (answer.crisis_type, *answer.crisis_secondary) if answer.crisis_type is not None else ()
    )
    if agent_types or classifier_types:
        response = crisis.respond(
            (*agent_types, *classifier_types),
            agent_reply=answer.reply if agent_types else None,
            primary=answer.crisis_type,
            prompt_set=prompt_set,
        )
        prompts.append(f"crisis.{response.template_version}")
        return DialogResult(
            level=5,
            strategy=STRATEGIES[5],
            reply=response.text,
            draft=None,
            crisis=response,
            calls=tuple(log.records),
            prompts=tuple(prompts),
            safety_error=safety_error,
        )

    level = min(answer.level, 4)  # уровень 5 без строки протокола — не кризис
    result_draft: Draft | None = None
    draft_error: str | None = None
    if with_draft and answer.draft_ready:
        try:
            result_draft = await draft(models, context, dialog, prompt_set=prompt_set, log=log)
        except LLMError as exc:
            draft_error = exc.kind
        prompts.append(system_prompt("draft", prompt_set).name)
    return DialogResult(
        level=level,
        strategy=STRATEGIES[level],
        reply=answer.reply,
        draft=result_draft,
        crisis=None,
        calls=tuple(log.records),
        prompts=tuple(prompts),
        draft_error=draft_error,
        safety_error=safety_error,
    )


def error_reply(exc: LLMError) -> str:
    """Текст для человека при ошибке модели (ТЗ §10: понятное сообщение, текст сохранён)."""
    if isinstance(exc, LLMRefusal):
        return REFUSAL_REPLY
    return exc.user_message

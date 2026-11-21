"""Кризисный протокол ТЗ §9.3 — ответ автору без смягчения (задача 0.5).

Текст протокола — фиксированные блоки ``prompts/crisis.v<N>.md`` (не генерируются моделью).
Код здесь решает, какие строки §9.3 сработали, в каком порядке идут блоки и какие действия
протокола выполнены. Партнёру о срабатывании не сообщается; событие — только тип, без текста."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, get_args

from app.ai.prompts import V0, PromptSet, load

CrisisType = Literal["violence", "outgoing_threat", "self_harm", "life_threat", "underage"]
CRISIS_TYPES: tuple[CrisisType, ...] = get_args(CrisisType)

# Порядок главной строки, если сработало несколько: младше 18 — главнее всех (доступ не
# предоставляется, но протокол остальных строк выполняется), затем угроза жизни.
PRIORITY: tuple[CrisisType, ...] = (
    "underage",
    "life_threat",
    "violence",
    "self_harm",
    "outgoing_threat",
)

# Действия, которые выполняет протокол строки (имена — evals/schema.json, $defs.crisis).
# Черновика нет и ничего не смягчается при любой строке — это обеспечивает код (dialog.py,
# pipeline.py), а не модель.
ACTIONS: dict[CrisisType, frozenset[str]] = {
    "violence": frozenset({"name_it", "no_tolerate", "help_contacts", "no_draft", "no_softening"}),
    "outgoing_threat": frozenset(
        {"not_sent", "no_softening", "explain_reason", "return_to_talk", "no_draft"}
    ),
    "self_harm": frozenset({"stay_in_conversation", "crisis_line_24h", "no_draft"}),
    "life_threat": frozenset({"emergency_services", "no_draft", "no_softening"}),
    "underage": frozenset({"no_access", "help_contacts", "no_draft", "no_softening"}),
}


@dataclass(frozen=True)
class CrisisResponse:
    """Ответ автору по протоколу. ``text`` — ответ агента (если был) и блоки шаблона."""

    primary: CrisisType
    secondary: tuple[CrisisType, ...]
    text: str
    actions: frozenset[str]
    template_version: str

    @property
    def types(self) -> tuple[CrisisType, ...]:
        return (self.primary, *self.secondary)


def order(types: Iterable[str]) -> tuple[CrisisType, ...]:
    """Строки §9.3 без повторов и неизвестных значений, главная — первой (по ``PRIORITY``)."""
    found = {t for t in types if t in CRISIS_TYPES}
    return tuple(t for t in PRIORITY if t in found)


def respond(
    types: Iterable[str],
    agent_reply: str | None = None,
    *,
    primary: str | None = None,
    prompt_set: PromptSet = V0,
) -> CrisisResponse:
    """Ответ по протоколу. ``primary`` — главная строка, если её назвал агент (иначе — по
    приоритету); ``underage`` всегда главная. Блоки: главная строка, затем вторичные; при
    ``underage`` блок отказа в доступе — последним, чтобы ответ не начинался с отказа."""
    ordered = order([*(types or ()), *([primary] if primary else [])])
    if not ordered:
        raise ValueError("нет строки кризисного протокола")
    main: CrisisType = ordered[0]
    if primary in CRISIS_TYPES and "underage" not in ordered:
        main = next(t for t in ordered if t == primary)
    rest = tuple(t for t in ordered if t != main)
    version = prompt_set.version("crisis")
    sequence = (*rest, main) if main == "underage" else (main, *rest)
    blocks = [load("crisis", version, block=t).text for t in sequence]
    parts = [agent_reply.strip()] if agent_reply and agent_reply.strip() else []
    parts += blocks
    actions = frozenset().union(*(ACTIONS[t] for t in ordered))
    return CrisisResponse(
        primary=main,
        secondary=rest,
        text="\n\n".join(parts),
        actions=actions,
        template_version=version,
    )

"""Кризисный протокол ТЗ §9.3 — ответ автору без смягчения (задача 0.5).

Текст протокола — фиксированные блоки ``prompts/crisis.v<N>.md`` (не генерируются моделью).
Код здесь решает, какие строки §9.3 сработали, в каком порядке идут блоки и какие действия
протокола выполнены. Партнёру о срабатывании не сообщается; событие — только тип, без текста."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, get_args

from app.ai.prompts import V0, PromptError, PromptSet, load

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


# Сообщение остановлено конвейером, а строки протокола обращены к пострадавшему (насилие,
# самоповреждение, угроза жизни): автор должен явно узнать, что текст не ушёл (§2 п.2, §9.3
# «объясняет причину»; DECISIONS АР-28). Текст — блок ``not_sent`` шаблона ``crisis.v1``
# (утверждают психолог и юрист, ревью R2-MINOR-1). В ``crisis.v0`` такого блока нет: для
# набора по умолчанию (V0 — все роли v0, АР-26) строка берётся из ``crisis.v1``, где она
# впервые появилась. У строки ``outgoing_threat`` «не отправлено» сказано в самом блоке.
NOT_SENT_SINCE = "v1"
NOT_SENT = load("crisis", NOT_SENT_SINCE, block="not_sent").text
NOT_SENT_ACTIONS = frozenset({"not_sent", "explain_reason"})
# Строки, блоки которых обращены к пострадавшему. Если главная строка ``outgoing_threat``
# (действует сам автор — в остановленном сообщении или по оценке агента в чате), их блоки и
# действия не добавляются:
# контролирующий или угрожающий автор не должен слышать «вина не на тебе» (АР-28, R2-MAJOR-1).
VICTIM_TYPES = frozenset({"violence", "life_threat"})


def not_sent_text(version: str) -> str:
    try:
        return load("crisis", version, block="not_sent").text
    except PromptError:
        return NOT_SENT


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
    withheld: bool = False,
) -> CrisisResponse:
    """Ответ по протоколу. ``primary`` — главная строка, если её назвал агент (иначе — по
    приоритету); ``underage`` всегда главная. Блоки: главная строка, затем вторичные; при
    ``underage`` блок отказа в доступе — последним, чтобы ответ не начинался с отказа.
    ``withheld`` — одобренное сообщение остановлено: если среди строк нет ``outgoing_threat``
    (её блок сам говорит «не отправлено»), первой идёт блок ``not_sent``. Если главная строка
    ``outgoing_threat`` — блоки ``VICTIM_TYPES`` не добавляются (строки остаются в
    ``secondary`` для события)."""
    ordered = order([*(types or ()), *([primary] if primary else [])])
    if not ordered:
        raise ValueError("нет строки кризисного протокола")
    main: CrisisType = ordered[0]
    if primary in CRISIS_TYPES and "underage" not in ordered:
        main = next(t for t in ordered if t == primary)
    rest = tuple(t for t in ordered if t != main)
    version = prompt_set.version("crisis")
    sequence = (*rest, main) if main == "underage" else (main, *rest)
    if main == "outgoing_threat":  # действует автор — без текста пострадавшему
        sequence = tuple(t for t in sequence if t not in VICTIM_TYPES)
    blocks = [load("crisis", version, block=t).text for t in sequence]
    parts = [agent_reply.strip()] if agent_reply and agent_reply.strip() else []
    actions = frozenset().union(*(ACTIONS[t] for t in sequence))
    if withheld and "outgoing_threat" not in ordered:
        parts.append(not_sent_text(version))
        actions |= NOT_SENT_ACTIONS
    parts += blocks
    return CrisisResponse(
        primary=main,
        secondary=rest,
        text="\n\n".join(parts),
        actions=actions,
        template_version=version,
    )

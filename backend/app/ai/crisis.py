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


# Блоки, которых нет в ранних версиях шаблона, и версия, где каждый впервые появился. Набор
# по умолчанию ``V0`` держит ``crisis.v0`` (все роли v0, тест A8 ``test_default_prompt_set_is_v0``);
# такие блоки для него берутся из версии, где они появились, а в записи результата видна версия
# каждого блока: ``template_version`` = «v0+not_sent@v1+author_safety@v2» (ревью R3-MINOR-2).
#
# - ``not_sent`` — «не отправлено», первым в ответе автору, если одобренное сообщение
#   остановлено, а строки протокола обращены к пострадавшему (АР-28, ревью MAJOR-1, R2-MINOR-1);
# - ``author_safety`` — нейтральная помощь, если главная строка ``outgoing_threat`` (действует
#   автор), а вторичная — ``violence``/``life_threat``: автор, возможно, сам пострадавший
#   (R3-MAJOR-1). Блоки для пострадавшего («вина не на тебе») такому автору не идут;
# - ``outgoing_threat_chat`` / ``outgoing_threat_mixed`` / ``outgoing_threat_mixed_chat`` —
#   строка ``outgoing_threat`` в личном чате (сообщения не было, без «не отправлено») и в
#   смешанном случае (без «подумаем, как сказать», §9.3 строка 1).
LATE_BLOCKS: dict[str, str] = {
    "not_sent": "v1",
    "author_safety": "v2",
    "outgoing_threat_chat": "v2",
    "outgoing_threat_mixed": "v2",
    "outgoing_threat_mixed_chat": "v2",
}
NOT_SENT_ACTIONS = frozenset({"not_sent", "explain_reason"})
AUTHOR_SAFETY_ACTIONS = frozenset({"help_contacts", "emergency_services"})
# Строки, блоки которых обращены к пострадавшему: при главной ``outgoing_threat`` вместо них —
# ``author_safety`` (АР-28, R2-MAJOR-1, R3-MAJOR-1).
VICTIM_TYPES = frozenset({"violence", "life_threat"})


def _block(version: str, name: str, used: dict[str, str]) -> str:
    """Текст блока версии ``version``; блок из ``LATE_BLOCKS``, которого в ней нет, — из версии,
    где он появился (записывается в ``used``). Прочие отсутствующие блоки — ``PromptError``."""
    try:
        return load("crisis", version, block=name).text
    except PromptError:
        since = LATE_BLOCKS.get(name)
        if since is None:
            raise
        used[name] = since
        return load("crisis", since, block=name).text


def __getattr__(name: str) -> str:
    """``NOT_SENT`` читается лениво (О-13): импорт модуля не зависит от наличия crisis.v1."""
    if name == "NOT_SENT":
        return load("crisis", LATE_BLOCKS["not_sent"], block="not_sent").text
    raise AttributeError(name)


@dataclass(frozen=True)
class CrisisResponse:
    """Ответ автору по протоколу. ``text`` — ответ агента (если был) и блоки шаблона."""

    primary: CrisisType
    secondary: tuple[CrisisType, ...]
    text: str
    actions: frozenset[str]
    template_version: str  # версия шаблона и, если были, блоки из других версий

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
    (её блок сам говорит «не отправлено»), первым идёт блок ``not_sent``.

    Главная ``outgoing_threat`` (действует автор): блоки ``VICTIM_TYPES`` не добавляются, но если
    такие строки есть, вместо них — ``author_safety`` (помощь без «вина не на тебе»), а блок
    ``outgoing_threat`` — смешанный, без «подумаем, как сказать». Вне остановленного сообщения
    (личный чат) — вариант блока без «сообщение не отправлено»."""
    ordered = order([*(types or ()), *([primary] if primary else [])])
    if not ordered:
        raise ValueError("нет строки кризисного протокола")
    main: CrisisType = ordered[0]
    if primary in CRISIS_TYPES and "underage" not in ordered:
        main = next(t for t in ordered if t == primary)
    rest = tuple(t for t in ordered if t != main)
    version = prompt_set.version("crisis")
    used: dict[str, str] = {}
    sequence = (*rest, main) if main == "underage" else (main, *rest)
    names: list[str] = list(sequence)
    actions = frozenset().union(*(ACTIONS[t] for t in sequence))
    if main == "outgoing_threat":  # действует автор — без текста пострадавшему
        victim = [t for t in sequence if t in VICTIM_TYPES]
        names = [t for t in sequence if t not in VICTIM_TYPES]
        actions = frozenset().union(*(ACTIONS[t] for t in sequence if t not in VICTIM_TYPES))
        mixed = "_mixed" if victim else ""
        chat = "" if withheld else "_chat"
        if mixed or chat:
            names[0] = f"outgoing_threat{mixed}{chat}"
        if not withheld:  # в чате сообщения не было — «не отправлено» было бы неправдой
            actions -= {"not_sent"}
        if victim:
            names.append("author_safety")
            actions |= AUTHOR_SAFETY_ACTIONS
    blocks = [_block(version, n, used) for n in names]
    parts = [agent_reply.strip()] if agent_reply and agent_reply.strip() else []
    if withheld and "outgoing_threat" not in ordered:
        parts.append(_block(version, "not_sent", used))
        actions |= NOT_SENT_ACTIONS
    parts += blocks
    label = version + "".join(f"+{n}@{v}" for n, v in used.items())
    return CrisisResponse(
        primary=main,
        secondary=rest,
        text="\n\n".join(parts),
        actions=actions,
        template_version=label,
    )

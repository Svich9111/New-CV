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
    # Блок violence велит при опасности звонить в экстренные службы (О-19).
    "violence": frozenset(
        {
            "name_it",
            "no_tolerate",
            "help_contacts",
            "emergency_services",
            "no_draft",
            "no_softening",
        }
    ),
    "outgoing_threat": frozenset(
        {"not_sent", "no_softening", "explain_reason", "return_to_talk", "no_draft"}
    ),
    "self_harm": frozenset({"stay_in_conversation", "crisis_line_24h", "no_draft"}),
    # Блок life_threat называет раздел «Помощь» — контакты помощи даются (раунд 4).
    "life_threat": frozenset({"emergency_services", "help_contacts", "no_draft", "no_softening"}),
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
    "author_safety_life": "v2",
    "outgoing_threat_brief": "v2",
    "outgoing_threat_brief_chat": "v2",
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
    # Имена блоков шаблона в порядке показа (без ответа агента) — ``plan().blocks``.
    blocks: tuple[str, ...] = ()

    @property
    def types(self) -> tuple[CrisisType, ...]:
        return (self.primary, *self.secondary)


def order(types: Iterable[str]) -> tuple[CrisisType, ...]:
    """Строки §9.3 без повторов и неизвестных значений, главная — первой (по ``PRIORITY``)."""
    found = {t for t in types if t in CRISIS_TYPES}
    return tuple(t for t in PRIORITY if t in found)


THREAT_BLOCKS = frozenset(
    {
        "outgoing_threat",
        "outgoing_threat_chat",
        "outgoing_threat_mixed",
        "outgoing_threat_mixed_chat",
        "outgoing_threat_brief",
        "outgoing_threat_brief_chat",
    }
)


@dataclass(frozen=True)
class CrisisPlan:
    """Что показать автору по протоколу — без текстов: главная и вторичные строки, имена блоков
    шаблона в порядке показа и действия. Тексты блоков — ``respond`` (или клиент по именам,
    АР-45, дополнение 3.4: лента отдаёт автору остановленного сообщения ``blocks``)."""

    primary: CrisisType
    secondary: tuple[CrisisType, ...]
    blocks: tuple[str, ...]
    actions: frozenset[str]


def respond(
    types: Iterable[str],
    agent_reply: str | None = None,
    *,
    primary: str | None = None,
    prompt_set: PromptSet = V0,
    withheld: bool = False,
) -> CrisisResponse:
    """Ответ по протоколу §9.3. Текст определяется **набором** строк и контекстом, а не главной
    строкой и не порядком вторичных (ревью 0.5 R4-MAJOR-1). Главная строка (``primary`` от
    агента, иначе по ``PRIORITY``; ``underage`` — всегда главная; у остановленного сообщения
    конвейер передаёт ``life_threat`` главной при этом признаке — Б-103) идёт в событие и в
    правиле «Б» задаёт блок после экстренного. Блоки и действия считает ``plan`` (без
    текстов), текст — блоки шаблона ``prompt_set``.

    Контекст: ``withheld = True`` — остановлено одобренное сообщение; ``False`` — личный чат
    (сообщения не было: ни «не отправлено», ни действия ``not_sent``).

    Таблица правил (T — есть ``outgoing_threat``; V — ``violence``; L — ``life_threat``;
    H — ``self_harm``; U — ``underage``). Ответ агента, если есть, — перед блоками.

    ==  =================  =====================================================================
    №   Условие            Блоки по порядку
    ==  =================  =====================================================================
    А   T (угрожает        0. ``not_sent`` — сообщение и есть H или U (короткий блок отправителю
        сам автор)            «не отправлено» не говорит);
                           1. ``author_safety_life``, если L — «немедленно звони в экстренные
                              службы» первым (§9.3 стр. 4, R5-MINOR-2);
                           2. ``self_harm``, если H — поддержка раньше любого блока
                              отправителю (§9.3 стр. 3, R5-MAJOR-1);
                           3. блок отправителю (одно сообщение / чат):
                              - есть H или U: ``outgoing_threat_brief`` / ``…_brief_chat`` — без
                                «вернёмся, когда станет спокойнее», без «подумаем, как сказать»,
                                без ``return_to_talk`` (R5-MAJOR-1, R5-MINOR-1);
                              - иначе есть V или L: ``outgoing_threat_mixed`` / ``…_mixed_chat``
                                — без «подумаем, как сказать» (§9.3 стр. 1);
                              - иначе: ``outgoing_threat`` / ``outgoing_threat_chat``;
                           4. ``author_safety``, если V и нет L; блоков пострадавшему
                              («вина не на тебе», ``name_it``, ``no_tolerate``) нет;
                           5. ``underage``, если U — последним.
    Б   нет T              0. ``not_sent``, если сообщение;
                           1. ``life_threat``, если L — первым (§9.3 стр. 4);
                           2. блок главной строки (если не L и не U);
                           3. остальные строки по ``PRIORITY``;
                           4. ``underage`` — последним, чтобы ответ не начинался с отказа.
    ==  =================  =====================================================================

    Действия: объединение действий показанных строк; в «А» при V или L — ещё ``help_contacts`` и
    ``emergency_services``, при H или U — без ``return_to_talk``; для сообщения —
    ``not_sent`` и ``explain_reason``; в чате ``not_sent`` нет никогда."""
    p = plan(types, primary=primary, withheld=withheld)
    version = prompt_set.version("crisis")
    used: dict[str, str] = {}
    parts = [agent_reply.strip()] if agent_reply and agent_reply.strip() else []
    parts += [_block(version, n, used) for n in p.blocks]
    label = version + "".join(f"+{n}@{v}" for n, v in used.items())
    return CrisisResponse(
        primary=p.primary,
        secondary=p.secondary,
        text="\n\n".join(parts),
        actions=p.actions,
        template_version=label,
        blocks=p.blocks,
    )


def plan(types: Iterable[str], *, primary: str | None = None, withheld: bool = False) -> CrisisPlan:
    """План ответа по протоколу §9.3 без текстов: главная и вторичные строки, имена блоков по
    порядку и действия — по таблице правил ``respond`` (там же — контекст ``withheld``). Лента
    пары вызывает ``plan`` с набором признаков и главным типом остановленного сообщения из БД
    (0021): те же блоки, что ``respond`` показал автору в конвейере."""
    ordered = order([*(types or ()), *([primary] if primary else [])])
    if not ordered:
        raise ValueError("нет строки кризисного протокола")
    main: CrisisType = ordered[0]
    if primary in CRISIS_TYPES and "underage" not in ordered:
        main = next(t for t in ordered if t == primary)
    rest = tuple(t for t in ordered if t != main)
    present = set(ordered)
    names: list[str] = []
    actions: frozenset[str]
    chat = "" if withheld else "_chat"
    if "outgoing_threat" in present:  # правило «А»
        danger = present & VICTIM_TYPES
        brief = bool(present & {"self_harm", "underage"})
        shown: list[CrisisType] = ["outgoing_threat"]
        if brief and withheld:
            names.append("not_sent")
        if "life_threat" in present:
            names.append("author_safety_life")
        if "self_harm" in present:
            names.append("self_harm")
            shown.append("self_harm")
        flavor = "_brief" if brief else "_mixed" if danger else ""
        names.append(f"outgoing_threat{flavor}{chat}")
        if "violence" in present and "life_threat" not in present:
            names.append("author_safety")
        if "underage" in present:
            names.append("underage")
            shown.append("underage")
        actions = frozenset().union(*(ACTIONS[t] for t in shown))
        if danger:
            actions |= AUTHOR_SAFETY_ACTIONS
        if brief:
            actions -= {"return_to_talk"}
    else:  # правило «Б»
        sequence: list[CrisisType] = []
        if "life_threat" in present:
            sequence.append("life_threat")
        sequence += [t for t in (main, *rest) if t not in ("underage", "life_threat")]
        if "underage" in present:
            sequence.append("underage")
        names = list(sequence)
        actions = frozenset().union(*(ACTIONS[t] for t in sequence))
        if withheld:
            names.insert(0, "not_sent")
            actions |= NOT_SENT_ACTIONS
    if not withheld:
        actions -= {"not_sent"}
    return CrisisPlan(primary=main, secondary=rest, blocks=tuple(names), actions=actions)

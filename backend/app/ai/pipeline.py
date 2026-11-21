"""Конвейер сообщения партнёру ТЗ §6.2 п.2–7 (задача 0.5; ФТ-08, ФТ-09, ФТ-10) — без БД.

1. **Одобрение** (п.2). ``approve`` — единственный способ получить ``ApprovedMessage``: только
   при явном подтверждении автора (в продукте — кнопка «Отправить», в прототипе — ответ «да»).
   Без него конвейер не запускается и ничего не доставляется (§2 п.2, ФТ-08).
2. **Безопасность** (п.3) — отдельный классификатор ``safety.v0`` по одобренному оригиналу.
   Сработал — сообщение не адаптируется и не доставляется (``withheld``); автору — ответ по
   кризисному протоколу (``crisis.v0``), событие — только тип, без текста; партнёру ничего.
3. **Тон** (п.4) — агент получателя, ``tone.v0``, шкала 0–3. 0–1 — доставка без изменений.
4. **Адаптация** (п.5) — при 2–3, ``adapt.v0``; пометку «адаптировано» ставит код.
5. **Контроль смысла** (п.6) — ``meaning.v0`` и сверка чисел кодом. Расхождение — получатель
   видит оригинал с предупреждением о резком тоне.
6. **Доставка** (п.7) — ``Delivery``: что увидит получатель, пометка, оригинал по запросу.

Изоляция (§6.2, последний абзац; §9.2): на вход ``process`` — только ``ApprovedMessage`` (текст
оригинала). Памяти, диалога и имён автора у конвейера нет по построению; в каждом вызове модели
— только оригинал (и адаптация — в контроле смысла). Текст сообщения — данные, не инструкция
(§9.2 п.3): он передаётся значением поля JSON, а пометку и решение о доставке принимает код.

Ошибки (§10, АР-20): ошибка или отказ модели на шагах безопасности и тона — сообщение не
доставляется (``LLMError`` вызывающему, текст у автора сохранён: безопасность не проверена — не
отправляем). На шагах адаптации и контроля смысла — оригинал с предупреждением: безопасность уже
проверена, а непроверенную адаптацию показывать нельзя (``degraded`` — вид ошибки)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from app.ai import crisis
from app.ai.calls import (
    CallLog,
    CallRecord,
    complete,
    enum_list,
    int_field,
    optional_text,
    strict_object,
    text_field,
)
from app.ai.dialog import MAX_TOKENS_CLASSIFIER, SafetyVerdict, classify
from app.ai.llm import LLMError
from app.ai.models import AgentModels
from app.ai.prompts import V0, PromptSet, system_prompt

Status = Literal["as_is", "adapted", "original_with_warning", "withheld"]

MAX_TOKENS_ADAPT = 1500
MAX_ORIGINAL_CHARS = 4000

TONE_SIGNALS = (
    "generalization",
    "counting",
    "exclamation",
    "sarcasm",
    "rhetorical_question",
    "accusation",
    "devaluation",
    "demand",
    "shouting",
)
MEANING_PROBLEMS = (
    "changed_facts",
    "added_facts",
    "dropped_request",
    "added_request",
    "added_promise",
    "added_apology",
    "added_blame",
    "dropped_feeling",
    "devalued_feelings",
)
# Категория классификатора сообщения → строка §9.3 для ответа автору.
MESSAGE_CRISIS: dict[str, crisis.CrisisType] = {
    "threat_or_insult": "outgoing_threat",
    "violence": "violence",
    "self_harm": "self_harm",
    "life_threat": "life_threat",
}

WARNING_HARSH_TONE = "Сообщение написано в резком тоне."
MARK_ADAPTED = "адаптировано"


class NotConfirmed(Exception):
    """Автор не подтвердил отправку — сообщение не уходит (§2 п.2, ФТ-08)."""


@dataclass(frozen=True)
class ApprovedMessage:
    """Оригинал, одобренный автором (§6.2 п.2). Создаётся только через ``approve``."""

    text: str
    _confirmed: bool = False

    def __post_init__(self) -> None:
        if self._confirmed is not True:
            raise NotConfirmed("сообщение не подтверждено автором")


def approve(text: str, *, confirmed: bool) -> ApprovedMessage:
    """Фиксирует оригинал. ``confirmed`` — явное «Отправить» автора (только ``True``)."""
    if confirmed is not True:
        raise NotConfirmed("без явного «Отправить» сообщение не уходит")
    clean = text.strip()
    if not clean:
        raise ValueError("пустое сообщение")
    if len(clean) > MAX_ORIGINAL_CHARS:
        raise ValueError(f"сообщение длиннее {MAX_ORIGINAL_CHARS} символов")
    return ApprovedMessage(clean, _confirmed=True)


@dataclass(frozen=True)
class Delivery:
    """Итог конвейера. ``shown`` — что увидит получатель (``None`` — ничего, ``withheld``)."""

    status: Status
    original: str  # одобренный оригинал: получателю — по запросу «Показать оригинал»
    shown: str | None
    is_adapted: bool
    harsh_tone_warning: bool
    tone: int | None
    tone_signals: tuple[str, ...]
    reply_hint: str | None
    safety_category: str
    meaning_problems: tuple[str, ...]
    author_notice: crisis.CrisisResponse | None  # ответ автору, если сообщение остановлено
    safety_event: str | None  # тип для safety_events — без текста (§9.3)
    calls: tuple[CallRecord, ...]
    prompts: tuple[str, ...]
    degraded: str | None = None  # ошибка адаптации/контроля смысла → оригинал с предупреждением

    @property
    def delivered(self) -> bool:
        return self.status != "withheld"

    @property
    def mark(self) -> str | None:
        return MARK_ADAPTED if self.is_adapted else None

    @property
    def cost_usd(self) -> float:
        return round(sum(c.cost_usd for c in self.calls), 8)


@dataclass(frozen=True)
class ToneVerdict:
    tone: int
    signals: tuple[str, ...]


@dataclass(frozen=True)
class Adaptation:
    adapted: str
    reply_hint: str | None


TONE_SCHEMA: dict[str, Any] = strict_object(
    {
        "tone": {"type": "integer", "enum": [0, 1, 2, 3]},
        "signals": {"type": "array", "items": {"type": "string", "enum": list(TONE_SIGNALS)}},
    }
)
ADAPT_SCHEMA: dict[str, Any] = strict_object(
    {
        "adapted": {"type": "string"},
        "reply_hint": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    }
)
# meaning.v0 — список видов; meaning.v1 — виды с дословными фрагментами (основание).
MEANING_SCHEMA_V0: dict[str, Any] = strict_object(
    {"problems": {"type": "array", "items": {"type": "string", "enum": list(MEANING_PROBLEMS)}}}
)
MEANING_SCHEMA_V1: dict[str, Any] = strict_object(
    {
        "problems": {
            "type": "array",
            "items": strict_object(
                {
                    "kind": {"type": "string", "enum": list(MEANING_PROBLEMS)},
                    "original_fragment": {"type": "string"},
                    "adapted_fragment": {"type": "string"},
                }
            ),
        }
    }
)


def _tone(data: dict[str, Any]) -> ToneVerdict:
    return ToneVerdict(int_field(data, "tone", range(4)), enum_list(data, "signals", TONE_SIGNALS))


def _adaptation(data: dict[str, Any]) -> Adaptation:
    return Adaptation(
        adapted=text_field(data, "adapted", allow_empty=True),
        reply_hint=optional_text(data, "reply_hint"),
    )


def meaning_schema(version: str) -> dict[str, Any]:
    return MEANING_SCHEMA_V0 if version == "v0" else MEANING_SCHEMA_V1


def _problems(data: dict[str, Any]) -> tuple[str, ...]:
    """Виды искажений (v0 — строки, v1 — объекты с фрагментами). Фрагменты — только основание
    для модели, в результат не идут: это тексты сообщения, а метрики — без текстов."""
    items = data["problems"]
    if not isinstance(items, list):
        raise TypeError("problems")
    kinds = []
    for item in items:
        kind = item.get("kind") if isinstance(item, dict) else item
        if not isinstance(kind, str) or kind not in MEANING_PROBLEMS:
            raise ValueError("problems")
        kinds.append(kind)
    return tuple(dict.fromkeys(kinds))


_NUMBER = re.compile(r"\d+")


def numbers_differ(original: str, adapted: str) -> bool:
    """Сверка чисел кодом: числа оригинала и адаптации должны совпадать (факты, §6.2 п.5)."""
    return sorted(_NUMBER.findall(original)) != sorted(_NUMBER.findall(adapted))


def _same_text(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


async def process(
    models: AgentModels, message: ApprovedMessage, *, prompt_set: PromptSet = V0
) -> Delivery:
    """Конвейер §6.2 п.3–7 по одобренному оригиналу (см. описание модуля)."""
    if not isinstance(message, ApprovedMessage):
        raise TypeError("конвейер принимает только ApprovedMessage (approve)")
    original = message.text
    log = CallLog()
    prompts = [system_prompt("safety", prompt_set).name]

    def result(**kwargs: Any) -> Delivery:
        base: dict[str, Any] = {
            "original": original,
            "tone": None,
            "tone_signals": (),
            "reply_hint": None,
            "safety_category": "none",
            "meaning_problems": (),
            "author_notice": None,
            "safety_event": None,
            "is_adapted": False,
            "harsh_tone_warning": False,
        }
        base.update(kwargs)
        return Delivery(calls=tuple(log.records), prompts=tuple(prompts), **base)

    # п.3 — безопасность: ошибка модели здесь — сообщение не уходит (LLMError вызывающему).
    verdict: SafetyVerdict = await classify(models, "message", original, log, prompt_set)
    types = [MESSAGE_CRISIS[c] for c in verdict.categories if c in MESSAGE_CRISIS]
    if types:  # «младше 18» по тексту сообщения — не строка конвейера (§9.3: это вход в сервис)
        notice = crisis.respond(
            types, primary=MESSAGE_CRISIS.get(verdict.category), prompt_set=prompt_set
        )
        prompts.append(f"crisis.{notice.template_version}")
        return result(
            status="withheld",
            shown=None,
            safety_category=verdict.category,
            author_notice=notice,
            safety_event=notice.primary,
        )

    # п.4 — тон (агент получателя, только оригинал).
    prompts.append(system_prompt("tone", prompt_set).name)
    tone: ToneVerdict = await complete(
        models.tone,
        prompt=system_prompt("tone", prompt_set),
        payload={"original": original},
        schema=TONE_SCHEMA,
        validate=_tone,
        log=log,
        max_tokens=MAX_TOKENS_CLASSIFIER,
    )
    if tone.tone <= 1:
        return result(status="as_is", shown=original, tone=tone.tone, tone_signals=tone.signals)

    harsh = {"tone": tone.tone, "tone_signals": tone.signals}

    def with_warning(problems: tuple[str, ...] = (), **extra: Any) -> Delivery:
        return result(
            status="original_with_warning",
            shown=original,
            harsh_tone_warning=True,
            meaning_problems=problems,
            **harsh,
            **extra,
        )

    # п.5 — адаптация формы.
    prompts.append(system_prompt("adapt", prompt_set).name)
    try:
        adaptation: Adaptation = await complete(
            models.adapt,
            prompt=system_prompt("adapt", prompt_set),
            payload={"original": original},
            schema=ADAPT_SCHEMA,
            validate=_adaptation,
            log=log,
            max_tokens=MAX_TOKENS_ADAPT,
        )
    except LLMError as exc:
        return with_warning(degraded=f"adapt:{exc.kind}")
    adapted = adaptation.adapted
    hint = adaptation.reply_hint
    if not adapted or _same_text(adapted, original):
        return with_warning(reply_hint=hint)

    # п.6 — контроль смысла: модель и сверка чисел кодом.
    prompts.append(system_prompt("meaning", prompt_set).name)
    try:
        problems: tuple[str, ...] = await complete(
            models.meaning,
            prompt=system_prompt("meaning", prompt_set),
            payload={"original": original, "adapted": adapted},
            schema=meaning_schema(prompt_set.version("meaning")),
            validate=_problems,
            log=log,
            max_tokens=MAX_TOKENS_CLASSIFIER,
        )
    except LLMError as exc:
        return with_warning(reply_hint=hint, degraded=f"meaning:{exc.kind}")
    if numbers_differ(original, adapted) and "changed_facts" not in problems:
        problems = (*problems, "changed_facts")
    if problems:
        return with_warning(problems, reply_hint=hint)

    # п.7 — доставка адаптации с пометкой; оригинал — по запросу.
    return result(status="adapted", shown=adapted, is_adapted=True, reply_hint=hint, **harsh)

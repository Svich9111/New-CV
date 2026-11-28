"""Конвейер сообщения партнёру ТЗ §6.2 п.2–7 (задача 0.5; ФТ-08, ФТ-09, ФТ-10) — без БД.

1. **Одобрение** (п.2). ``approve`` — единственный способ получить ``ApprovedMessage``: только
   при явном подтверждении автора (в продукте — кнопка «Отправить», в прототипе — ответ «да»).
   Без него конвейер не запускается и ничего не доставляется (§2 п.2, ФТ-08).
2. **Безопасность** (п.3) — отдельный классификатор ``safety`` по одобренному оригиналу.
   Сработал — сообщение не адаптируется и не доставляется (``withheld``, допущение АР-28); автор
   явно узнаёт «не отправлено» и получает ответ по кризисному протоколу (``crisis``); событие —
   только тип, без текста; партнёру ничего. «Младше 18» по тексту сообщения конвейер не
   останавливает, но событие ``underage`` возвращается (проверка доступа — вне конвейера).
3. **Тон** (п.4) — агент получателя, ``tone.v0``, шкала 0–3. 0–1 — доставка без изменений.
4. **Адаптация** (п.5) — при 2–3, ``adapt.v0``; пометку «адаптировано» ставит код.
5. **Контроль смысла** (п.6) — ``meaning`` и сверка чисел кодом (``facts``). Расхождение —
   получатель видит оригинал с предупреждением о резком тоне, **без** подсказки агента.
6. **Доставка** (п.7) — ``Delivery``: что увидит получатель, пометка, оригинал по запросу.

Изоляция (§6.2, последний абзац; §9.2): на вход ``process`` — только ``ApprovedMessage`` (текст
оригинала). Памяти, диалога и имён автора у конвейера нет по построению; в каждом вызове модели
— только оригинал (и адаптация — в контроле смысла). Текст сообщения — данные, не инструкция
(§9.2 п.3): он передаётся значением поля JSON, а пометку и решение о доставке принимает код.

Подсказка получателю (``reply_hint``, §4.4 п.5) — голос агента получателя, поэтому проверяется:
не показывается при оригинале с предупреждением; кодом — длина и отсутствие дословных кусков
оригинала (внедрённая в текст команда не «переезжает» в подсказку); ``meaning.v2`` — ещё и
моделью (вид ``hint_off``: подсказка снимается, адаптация остаётся).

Ошибки (§10, АР-20, АР-27): любая ошибка модели на шагах безопасности и тона — сообщение не
доставляется (``LLMError`` вызывающему, текст у автора сохранён). На адаптации и контроле
смысла недоступность и испорченный ответ — оригинал с предупреждением (``degraded`` — вид
ошибки); отказ модели и прочие ошибки — не доставляется, ``LLMError`` вызывающему."""

from __future__ import annotations

import hashlib
import re
import unicodedata
import weakref
from dataclasses import dataclass, field
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
from app.ai.facts import numbers_differ
from app.ai.llm import LLMError, LLMRefusal
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
# Виды искажений meaning.v0 / v1 и добавленные в meaning.v2 (ревью 0.5 MAJOR-4, MAJOR-5).
MEANING_PROBLEMS_V0 = (
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
MEANING_PROBLEMS_V2 = ("added_feeling", "hint_off")
MEANING_PROBLEMS = (*MEANING_PROBLEMS_V0, *MEANING_PROBLEMS_V2)
# «hint_off» касается только подсказки: адаптация доставляется, подсказка — нет.
HINT_ONLY_PROBLEMS = frozenset({"hint_off"})
# Ошибки адаптации и контроля смысла, при которых доставляется оригинал с предупреждением
# (АР-27); остальные (отказ, ключ, лимит, некорректный запрос, модель не найдена — ошибка
# настройки, решение A0 по ревью R2-MINOR-3) — не доставляется.
DEGRADE_KINDS = frozenset({"unavailable", "bad_output"})
MAX_HINT_CHARS = 400
HINT_COPY_WORDS = 5  # столько подряд слов оригинала в подсказке — подсказка не показывается
# Категория классификатора сообщения → строка §9.3 для ответа автору.
MESSAGE_CRISIS: dict[str, crisis.CrisisType] = {
    "threat_or_insult": "outgoing_threat",
    "violence": "violence",
    "self_harm": "self_harm",
    "life_threat": "life_threat",
}


def message_primary(category: str, types: list[crisis.CrisisType]) -> crisis.CrisisType | None:
    """Главная строка §9.3 остановленного сообщения (Б-103): при признаке угрозы жизни —
    ``life_threat`` кодом, а не только по промпту (``safety.v2``: «underage, затем life_threat»;
    ``underage`` у сообщения не строка конвейера); иначе — категория классификатора, если она
    строка конвейера (``None`` — главная по ``crisis.PRIORITY``). Блоки ответа от этого не
    меняются (АР-57 п.4): категория модели стоит в ``types`` первой, и ``crisis.plan`` при
    главной ``life_threat`` ставит её блок после экстренного, как до Б-103."""
    if "life_threat" in types:
        return "life_threat"
    return MESSAGE_CRISIS.get(category)


WARNING_HARSH_TONE = "Сообщение написано в резком тоне."
MARK_ADAPTED = "адаптировано"


class NotConfirmed(Exception):
    """Автор не подтвердил отправку — сообщение не уходит (§2 п.2, ФТ-08)."""


# Выданные ``approve`` объекты (по идентичности): ``process`` принимает только их, поэтому
# ``dataclasses.replace``, ``object.__new__`` и прямой конструктор не подделают одобрение
# (A8-1). В продукте ту же гарантию даёт статус черновика в БД.
#
# Запись реестра хранит и хеш одобренного текста: подмена ``text`` через ``object.__setattr__``
# после одобрения не проходит (A8-5). Состояние: ``approved`` → ``processing`` → ``used``; одно
# одобрение — одна доставка (§2 п.2: одно «Отправить» — одна отправка, A8-6). Решение конвейера
# (доставлено или остановлено безопасностью) и отказ модели (АР-27, R3-MINOR-3) расходуют
# одобрение; прочий сбой (недоступность, испорченный ответ, ошибка настройки, отмена) — нет:
# повтор того же одобрения после него разрешён (текст у автора сохранён, §10). Два одновременных
# ``process`` одного одобрения не идут: второй получает ``AlreadyProcessed``. Объект одобрения
# не переживает границу процесса: в 3.2 воркер вызывает ``approve()`` сам после проверки
# статуса черновика в БД (BACKLOG Б-48).


class AlreadyProcessed(ValueError):
    """Одобрение уже израсходовано доставкой или обрабатывается сейчас."""


@dataclass
class _Entry:
    ref: weakref.ref[ApprovedMessage]
    digest: str
    state: Literal["approved", "processing", "used"] = "approved"


_ISSUED: dict[int, _Entry] = {}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class ApprovedMessage:
    """Оригинал, одобренный автором (§6.2 п.2). Создаётся только через ``approve``."""

    text: str
    _confirmed: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self._confirmed is not True:
            raise NotConfirmed("сообщение не подтверждено автором")
        _check_text(self.text)

    @property
    def issued(self) -> bool:
        """Выдан ``approve()``, текст не менялся после одобрения."""
        entry = _ISSUED.get(id(self))
        return (
            entry is not None
            and entry.ref() is self
            and isinstance(self.text, str)
            and _digest(self.text) == entry.digest
        )


def _check_text(text: object) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("пустое сообщение")
    if text != text.strip():
        raise ValueError("текст не нормализован (approve)")
    if len(text) > MAX_ORIGINAL_CHARS:
        raise ValueError(f"сообщение длиннее {MAX_ORIGINAL_CHARS} символов")


def approve(text: str, *, confirmed: bool) -> ApprovedMessage:
    """Фиксирует оригинал. ``confirmed`` — явное «Отправить» автора (только ``True``)."""
    if confirmed is not True:
        raise NotConfirmed("без явного «Отправить» сообщение не уходит")
    if not isinstance(text, str):
        raise TypeError("текст сообщения — строка")
    clean = text.strip()
    _check_text(clean)
    message = ApprovedMessage(clean, _confirmed=True)
    key = id(message)

    def forget(_ref: weakref.ref[ApprovedMessage]) -> None:
        _ISSUED.pop(key, None)

    _ISSUED[key] = _Entry(weakref.ref(message, forget), _digest(clean))
    return message


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
    safety_event: str | None  # главный тип для safety_events — без текста (§9.3)
    calls: tuple[CallRecord, ...]
    prompts: tuple[str, ...]
    degraded: str | None = None  # ошибка адаптации/контроля смысла → оригинал с предупреждением
    hint_dropped: str | None = None  # почему подсказка не показана: warning | guard | hint_off
    safety_events: tuple[str, ...] = ()  # все события без текста (главное — первым)

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
# meaning.v0 — список видов; v1 — виды с дословными фрагментами (основание); v2 — то же плюс
# «добавлено чувство» и «подсказка не по делу», на входе ещё и подсказка.
MEANING_SCHEMA_V0: dict[str, Any] = strict_object(
    {
        "problems": {
            "type": "array",
            "items": {"type": "string", "enum": list(MEANING_PROBLEMS_V0)},
        }
    }
)


def _objects_schema(kinds: tuple[str, ...]) -> dict[str, Any]:
    return strict_object(
        {
            "problems": {
                "type": "array",
                "items": strict_object(
                    {
                        "kind": {"type": "string", "enum": list(kinds)},
                        "original_fragment": {"type": "string"},
                        "adapted_fragment": {"type": "string"},
                    }
                ),
            }
        }
    )


MEANING_SCHEMA_V1: dict[str, Any] = _objects_schema(MEANING_PROBLEMS_V0)
MEANING_SCHEMA_V2: dict[str, Any] = _objects_schema(MEANING_PROBLEMS)


def _tone(data: dict[str, Any]) -> ToneVerdict:
    return ToneVerdict(int_field(data, "tone", range(4)), enum_list(data, "signals", TONE_SIGNALS))


def _adaptation(data: dict[str, Any]) -> Adaptation:
    return Adaptation(
        adapted=text_field(data, "adapted", allow_empty=True),
        reply_hint=optional_text(data, "reply_hint"),
    )


def meaning_schema(version: str) -> dict[str, Any]:
    return {"v0": MEANING_SCHEMA_V0, "v1": MEANING_SCHEMA_V1}.get(version, MEANING_SCHEMA_V2)


def meaning_checks_hint(version: str) -> bool:
    """С ``meaning.v2`` контроль смысла получает и проверяет подсказку получателю."""
    return version not in ("v0", "v1")


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


_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _guard_words(text: str) -> list[str]:
    """Слова для сравнения подсказки с оригиналом: NFKC, без невидимых символов формата
    (нулевой ширины и т. п., категория Cf) и комбинируемых знаков (Mn: ударения), ё → е, без
    регистра; пунктуация — разделитель (A8-4, A8-8). «Й» после NFD теряет кратку — сравнение
    одинаково для обоих текстов. Латинские двойники букв не сводятся (О-12, предел эвристики)."""
    text = unicodedata.normalize("NFKC", text)
    # Комбинируемые знаки (ударение U+0301 и т. п., Mn) разбивали слово на части (A8-8).
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cf", "Mn"))
    text = unicodedata.normalize("NFC", text)
    text = text.casefold().replace("ё", "е")
    return _WORD.findall(text)


def hint_guard(hint: str | None, original: str) -> str | None:
    """Проверка подсказки кодом: ``None`` — показывать можно, иначе причина. Подсказка не
    должна быть длинной и не должна нести дословные куски оригинала (так внедрённая в текст
    команда «агенту» не становится словами агента получателя, §9.2 п.3)."""
    if hint is None:
        return None
    if len(hint) > MAX_HINT_CHARS:
        return "guard"
    words = _guard_words(original)
    hint_words = " " + " ".join(_guard_words(hint)) + " "
    for i in range(len(words) - HINT_COPY_WORDS + 1):
        if " " + " ".join(words[i : i + HINT_COPY_WORDS]) + " " in hint_words:
            return "guard"
    return None


def _same_text(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


async def process(
    models: AgentModels, message: ApprovedMessage, *, prompt_set: PromptSet = V0
) -> Delivery:
    """Конвейер §6.2 п.3–7 по одобренному оригиналу (см. описание модуля). Одно одобрение —
    одно решение конвейера; после сбоя модели одобрение можно передать снова."""
    if not isinstance(message, ApprovedMessage) or not message.issued:
        raise TypeError("конвейер принимает только ApprovedMessage, выданный approve()")
    _check_text(message.text)
    entry = _ISSUED[id(message)]
    if entry.state != "approved":
        raise AlreadyProcessed("это одобрение уже обработано: нужно новое «Отправить»")
    entry.state = "processing"
    try:
        delivery = await _process(models, message.text, prompt_set)
    except LLMRefusal:
        # Отказ модели — решение «не доставлять» (АР-27): одобрение израсходовано, повтор того же
        # одобрения не превратит отказ в доставку; нужно новое «Отправить» (ревью R3-MINOR-3).
        entry.state = "used"
        raise
    except BaseException:
        entry.state = "approved"  # сбой — одобрение не израсходовано, повтор разрешён
        raise
    entry.state = "used"
    return delivery


async def _process(models: AgentModels, original: str, prompt_set: PromptSet) -> Delivery:
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
    if verdict.category not in MESSAGE_CRISIS:
        # Категория модели — не строка конвейера: порядок вторичных от модели не значим
        # (первая строка набора задаёт порядок блоков только как категория модели, Б-103).
        types = list(crisis.order(types))
    if types:
        notice = crisis.respond(
            types,
            primary=message_primary(verdict.category, types),
            prompt_set=prompt_set,
            withheld=True,
        )
        prompts.append(f"crisis.{notice.template_version}")
        return result(
            status="withheld",
            shown=None,
            safety_category=verdict.category,
            author_notice=notice,
            safety_event=notice.primary,
            # «Младше 18» не теряется и при остановке — отдельное событие (A8-7).
            safety_events=(
                notice.primary,
                *(("underage",) if "underage" in verdict.categories else ()),
            ),
        )

    # «Младше 18» по тексту сообщения — не строка конвейера (§9.3: это вход в сервис), но
    # сигнал не теряется: событие без текста для проверки доступа (ревью 0.5 MINOR-2).
    age_event = "underage" if "underage" in verdict.categories else None
    base_event: dict[str, Any] = {
        "safety_event": age_event,
        "safety_events": (age_event,) if age_event else (),
    }

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
        return result(
            status="as_is", shown=original, tone=tone.tone, tone_signals=tone.signals, **base_event
        )

    harsh = {"tone": tone.tone, "tone_signals": tone.signals, **base_event}

    def with_warning(problems: tuple[str, ...] = (), **extra: Any) -> Delivery:
        # Подсказка агента получателя к отклонённой или непроверенной адаптации не показывается.
        dropped = "warning" if extra.pop("hint", None) else None
        return result(
            status="original_with_warning",
            shown=original,
            harsh_tone_warning=True,
            meaning_problems=problems,
            hint_dropped=dropped,
            **harsh,
            **extra,
        )

    def degrade(step: str, exc: LLMError, hint: str | None = None) -> Delivery:
        if exc.kind not in DEGRADE_KINDS:  # отказ и прочее — не доставляется (АР-27)
            raise exc
        # Подсказка к непроверенной адаптации не показывается — причина в метрике (A8-9).
        return with_warning(degraded=f"{step}:{exc.kind}", hint=hint)

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
        return degrade("adapt", exc)
    adapted = adaptation.adapted
    hint = adaptation.reply_hint
    if not adapted or _same_text(adapted, original):
        return with_warning(hint=hint)

    # п.6 — контроль смысла: модель и сверка чисел кодом.
    prompts.append(system_prompt("meaning", prompt_set).name)
    version = prompt_set.version("meaning")
    payload: dict[str, Any] = {"original": original, "adapted": adapted}
    if meaning_checks_hint(version):
        payload["reply_hint"] = hint
    try:
        problems: tuple[str, ...] = await complete(
            models.meaning,
            prompt=system_prompt("meaning", prompt_set),
            payload=payload,
            schema=meaning_schema(version),
            validate=_problems,
            log=log,
            max_tokens=MAX_TOKENS_CLASSIFIER,
        )
    except LLMError as exc:
        return degrade("meaning", exc, hint)
    if numbers_differ(original, adapted) and "changed_facts" not in problems:
        problems = (*problems, "changed_facts")
    if set(problems) - HINT_ONLY_PROBLEMS:
        return with_warning(problems, hint=hint)

    # п.7 — доставка адаптации с пометкой; оригинал — по запросу. Подсказка — после проверок.
    dropped = "hint_off" if "hint_off" in problems and hint else hint_guard(hint, original)
    return result(
        status="adapted",
        shown=adapted,
        is_adapted=True,
        reply_hint=None if dropped else hint,
        hint_dropped=dropped,
        meaning_problems=problems,
        **harsh,
    )

"""Адаптер Claude к эталонному набору (задача 0.4): ``--model adapters.claude:Opus`` и т. п.

Работает только через слой ``backend/app/ai/llm/`` (SDK напрямую не импортируется).
Реализует интерфейс ``evals/run.py`` по шагам:

1. ``respond`` — **агент владельца**: только имена пары, отношения, память и реплики владельца
   и одобренное сообщение пары (ТЗ §6.1, §9.2 п.1). Ответ: уровень, стратегия, ответ,
   черновик, кризис.
2. ``deliver`` — **агент получателя**, отдельный вызов **только по оригиналу**: ни памяти, ни
   реплик, ни имён автора (ТЗ §6.2, последний абзац; ревью 0.3 MAJOR-1). Ответ: проверка
   безопасности, тон, адаптация, подсказка получателю. Решение о доставке выводит код ниже.
3. ``converse`` — тот же агент владельца, но ответ после каждой реплики (многоходовый диалог,
   ключевой сценарий §1): история разговора — его же прошлые ответы.

Что адаптер **не** выдумывает (ревью 0.3 MAJOR-3): ``safety_event`` ставится, только если
модель сама вернула кризис; флаг безопасности агента получателя передаётся как
``safety_flag``; действия протокола — ровно те, что вернула модель; ``partner_notified`` всегда
``False`` (пути уведомить партнёра нет). «Кризис распознан» (ТЗ §12 п.6) — только по оценке
эксперта.

Ошибки: отказ модели, недоступность, испорченный ответ — ``ModelCallFailed`` (ситуация «не
прошла», прогон идёт); ключ, настройка, некорректный запрос — ``RunAborted`` (прогон
останавливается). Метрики на ситуацию (время, токены, стоимость) — через ``pop_metrics()``.
Без ``ANTHROPIC_API_KEY`` конструктор бросает ``LLMConfigError`` — прогон не начинается.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

from app.ai.llm import (
    HAIKU,
    OPUS,
    SONNET,
    ChatMessage,
    ClaudeProvider,
    LLMError,
    LLMProvider,
    StructuredResult,
    Usage,
)

EVALS_DIR = Path(__file__).resolve().parent.parent
PROMPT_VERSION = "compare.v1"
PROMPT_PATH = EVALS_DIR.parent / "prompts" / f"{PROMPT_VERSION}.md"

STRATEGIES = (
    "listen_and_request",
    "find_need_prepare_message",
    "deescalate_live_talk",
    "support_recommend_therapist",
    "crisis_protocol",
)
SAFETY = ("none", "threat_or_insult", "violence_signs")
# Ошибки, при которых прогон бессмыслен: продолжать на остальных ситуациях нельзя.
FATAL_KINDS = frozenset({"config", "auth", "bad_request", "quota"})


class ModelCallFailed(Exception):
    """Модель не ответила на ситуацию (отказ, недоступность, испорченный ответ)."""


class RunAborted(Exception):
    """Фатальная ошибка: прогон останавливается (run.py проверяет ``abort_run``)."""

    abort_run = True


def _schema_enum(*path: str) -> list[str]:
    node: Any = json.loads((EVALS_DIR / "schema.json").read_text(encoding="utf-8"))
    for key in path:
        node = node[key]
    return list(node["enum"])


CRISIS_TYPES = _schema_enum("$defs", "crisis", "properties", "type")
CRISIS_ACTIONS = _schema_enum("$defs", "crisis", "properties", "actions", "items")


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


OWNER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "level": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "strategy": {"type": "string", "enum": list(STRATEGIES)},
        "reply": {"type": "string"},
        "draft": _nullable({"type": "string"}),
        "crisis_type": _nullable({"type": "string", "enum": CRISIS_TYPES}),
        "crisis_actions": {"type": "array", "items": {"type": "string", "enum": CRISIS_ACTIONS}},
    },
    "required": ["level", "strategy", "reply", "draft", "crisis_type", "crisis_actions"],
    "additionalProperties": False,
}

RECIPIENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "safety": {"type": "string", "enum": list(SAFETY)},
        "tone": {"type": "integer", "enum": [0, 1, 2, 3]},
        "adapted": _nullable({"type": "string"}),
        "reply_hint": _nullable({"type": "string"}),
    },
    "required": ["safety", "tone", "adapted", "reply_hint"],
    "additionalProperties": False,
}


def load_prompt(role: str, path: Path = PROMPT_PATH) -> str:
    """Блок ``<!-- prompt:role --> … <!-- /prompt:role -->`` из файла промпта."""
    text = path.read_text(encoding="utf-8")
    match = re.search(
        rf"<!-- prompt:{role} -->\n(.*?)\n<!-- /prompt:{role} -->", text, flags=re.DOTALL
    )
    if not match:
        raise ValueError(f"{path.name}: нет блока prompt:{role}")
    return match.group(1).strip()


def owner_payload(request: Any, turns: Sequence[str] | None = None) -> dict[str, Any]:
    """Вход агента владельца: только поля EvalRequest (данные владельца и сообщение пары)."""
    pm = request.partner_message
    return {
        "owner_name": request.owner_name,
        "partner_name": request.partner_name,
        "relationship": request.relationship,
        "owner_memory": list(request.owner_memory),
        "owner_turns": list(request.owner_turns if turns is None else turns),
        "approved_partner_message": (
            {"direction": pm.direction, "text": pm.original} if pm is not None else None
        ),
    }


def recipient_payload(original: str) -> dict[str, Any]:
    """Вход агента получателя — только одобренный оригинал (ТЗ §6.2)."""
    return {"original": original}


def decide_delivery(original: str, data: dict[str, Any]) -> tuple[str, str | None]:
    """Решение конвейера §6.2 п.3–5 по ответу агента получателя."""
    if data.get("safety", "none") != "none":
        return "withheld", None  # п.3: не адаптируется и не смягчается
    adapted = data.get("adapted")
    if int(data["tone"]) >= 2:
        if isinstance(adapted, str) and adapted.strip() and adapted.strip() != original.strip():
            return "adapted", adapted
        return "original_with_warning", None
    return "as_is", None


def _as_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _tokens_in(usage: Usage | None) -> int:
    if usage is None:
        return 0
    return usage.input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens


def _run_module(request: Any) -> ModuleType:
    """Модуль run.py, из которого пришёл запрос (``__main__`` или загруженный в тестах)."""
    return sys.modules[type(request).__module__]


class ClaudeAdapter:
    """Базовый адаптер. Подклассы задают модель; провайдер можно подменить в тестах."""

    model_id: str = OPUS
    name: str = "claude"

    def __init__(
        self,
        provider: LLMProvider | None = None,
        *,
        provider_factory: Callable[[str], LLMProvider] | None = None,
    ) -> None:
        factory = provider_factory or (lambda model: ClaudeProvider(model))
        self.provider = provider if provider is not None else factory(self.model_id)
        self.owner_system = load_prompt("owner")
        self.recipient_system = load_prompt("recipient")
        self._runner = asyncio.Runner()
        self._calls: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ вызовы модели

    def _call(
        self, role: str, system: str, messages: list[ChatMessage], schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Один вызов; результат или ошибка записываются в метрики (без текстов)."""
        coro = self.provider.complete_json(system=system, messages=messages, schema=schema)
        try:
            result: StructuredResult = self._runner.run(coro)
        except LLMError as exc:
            # Ответ мог быть получен и оплачен (отказ, обрезанный ответ) — расход учитывается.
            usage, cost = exc.usage, exc.cost_usd
            self._calls.append(
                {
                    "role": role,
                    "model": self.provider.model,
                    "error": exc.kind,
                    "input_tokens": _tokens_in(usage),
                    "output_tokens": usage.output_tokens if usage else 0,
                    "cost_usd": cost or 0.0,
                }
            )
            if exc.kind in FATAL_KINDS:
                raise RunAborted(f"{exc.kind}: {exc.detail}") from exc
            raise ModelCallFailed(f"{role}: {exc.kind}") from exc
        self._calls.append(
            {
                "role": role,
                "model": result.model,
                "latency_s": result.latency_s,
                "input_tokens": _tokens_in(result.usage),
                "output_tokens": result.usage.output_tokens,
                "cost_usd": result.cost_usd,
                "fallback_used": result.fallback_used,
                "error": None,
            }
        )
        return result.data

    def _owner_response(self, request: Any, data: dict[str, Any]) -> Any:
        crisis_type = data.get("crisis_type")
        return _run_module(request).EvalResponse(
            level=int(data["level"]),
            strategy=str(data["strategy"]),
            reply=str(data["reply"]),
            draft=data.get("draft") or None,
            crisis_type=crisis_type,
            crisis_actions=frozenset(data.get("crisis_actions", [])),
            safety_event=crisis_type is not None,
            partner_notified=False,
        )

    # ------------------------------------------------------------------ интерфейс run.py

    def respond(self, request: Any) -> Any:
        """Агент владельца — один вызов по всем репликам."""
        user = ChatMessage("user", _as_json(owner_payload(request)))
        return self._owner_response(
            request, self._call("owner", self.owner_system, [user], OWNER_SCHEMA)
        )

    def converse(self, request: Any) -> list[Any]:
        """Агент владельца по шагам: ответ после каждой реплики, история — его прошлые ответы."""
        turns = list(request.owner_turns)
        history: list[ChatMessage] = []
        responses = []
        for i, turn in enumerate(turns):
            content = (
                _as_json(owner_payload(request, turns[:1]))
                if i == 0
                else _as_json({"owner_turn": turn})
            )
            history.append(ChatMessage("user", content))
            data = self._call("owner_step", self.owner_system, list(history), OWNER_SCHEMA)
            history.append(ChatMessage("assistant", json.dumps(data, ensure_ascii=False)))
            responses.append(self._owner_response(request, data))
        return responses

    def deliver(self, request: Any) -> Any:
        """Агент получателя — отдельный вызов только по оригиналу."""
        original = request.original
        user = ChatMessage("user", _as_json(recipient_payload(original)))
        data = self._call("recipient", self.recipient_system, [user], RECIPIENT_SCHEMA)
        delivery, adapted = decide_delivery(original, data)
        flagged = data.get("safety", "none") != "none"
        hint = data.get("reply_hint") if delivery != "withheld" else None
        return _run_module(request).DeliveryResponse(
            delivery=delivery,
            tone=int(data["tone"]),
            adapted=adapted,
            safety_flag=flagged,
            reply_hint=hint or None,
        )

    def pop_metrics(self) -> dict[str, Any]:
        calls, self._calls = self._calls, []
        ok = [c for c in calls if c["error"] is None]
        return {
            "prompt": PROMPT_VERSION,
            "calls": calls,
            "latency_s": round(sum(c["latency_s"] for c in ok), 3),
            # Токены и стоимость — по всем вызовам, включая неудачные (оплаченный отказ и т. п.).
            "input_tokens": sum(c["input_tokens"] for c in calls),
            "output_tokens": sum(c["output_tokens"] for c in calls),
            "cost_usd": round(sum(c["cost_usd"] for c in calls), 8),
            "errors": [c["error"] for c in calls if c["error"] is not None],
        }

    def close(self) -> None:
        self._runner.close()


class Opus(ClaudeAdapter):
    model_id = OPUS
    name = OPUS


class Sonnet(ClaudeAdapter):
    model_id = SONNET
    name = SONNET


class Haiku(ClaudeAdapter):
    model_id = HAIKU
    name = HAIKU

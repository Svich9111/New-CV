"""Адаптер Claude к эталонному набору (задача 0.4): ``--model adapters.claude:Opus`` и т. п.

Работает только через слой ``backend/app/ai/llm/`` (SDK напрямую не импортируется).

На ситуацию — до двух отдельных вызовов модели:

1. **Агент владельца** — только имена пары, отношения, память и реплики владельца и одобренное
   сообщение пары (ТЗ §6.1, §9.2 п.1). Ответ: уровень, стратегия, ответ, черновик, кризис.
2. **Агент получателя** — только для ситуаций с сообщением пары и **только по оригиналу**: ни
   памяти, ни реплик, ни имён автора (ТЗ §6.2, последний абзац; ревью 0.3 MAJOR-1). Ответ:
   проверка безопасности, тон, адаптация. Решение о доставке выводит код ниже, а не модель.

Что адаптер **не** выдумывает (ревью 0.3 MAJOR-3): ``safety_event`` ставится, только если
модель сама вернула кризис (агент владельца) или флаг безопасности (агент получателя);
действия протокола — ровно те, что вернула модель. ``partner_notified`` всегда ``False``: у
адаптера нет пути уведомить партнёра. Поэтому «кризис распознан» (ТЗ §12 п.6) считается
только по оценке эксперта, а не по этим полям.

Метрики на ситуацию (время, токены, стоимость по прайсу) отдаются через ``pop_metrics()``.
Без ``ANTHROPIC_API_KEY`` конструктор бросает ``LLMConfigError`` — прогон не начинается.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
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
)

EVALS_DIR = Path(__file__).resolve().parent.parent
PROMPT_PATH = EVALS_DIR.parent / "prompts" / "compare.v0.md"
PROMPT_VERSION = "compare.v0"

STRATEGIES = (
    "listen_and_request",
    "find_need_prepare_message",
    "deescalate_live_talk",
    "support_recommend_therapist",
    "crisis_protocol",
)
SAFETY = ("none", "threat_or_insult", "violence_signs")
# Ошибки, при которых прогон бессмыслен: продолжать на остальных ситуациях нельзя.
FATAL_KINDS = frozenset({"config", "auth", "bad_request"})


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
    },
    "required": ["safety", "tone", "adapted"],
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


def owner_payload(request: Any) -> dict[str, Any]:
    """Вход агента владельца: только поля EvalRequest (данные владельца и сообщение пары)."""
    pm = request.partner_message
    return {
        "owner_name": request.owner_name,
        "partner_name": request.partner_name,
        "relationship": request.relationship,
        "owner_memory": list(request.owner_memory),
        "owner_turns": list(request.owner_turns),
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
        return "withheld", None  # п.3: не адаптируется
    adapted = data.get("adapted")
    if int(data["tone"]) >= 2:
        if isinstance(adapted, str) and adapted.strip() and adapted.strip() != original.strip():
            return "adapted", adapted
        return "original_with_warning", None
    return "as_is", None


def _as_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


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

    def _call(self, role: str, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Один вызов; ошибка записывается в метрики. Фатальные — пробрасываются."""
        coro = self.provider.complete_json(
            system=system, messages=[ChatMessage("user", user)], schema=schema
        )
        try:
            result: StructuredResult = self._runner.run(coro)
        except LLMError as exc:
            self._calls.append({"role": role, "model": self.provider.model, "error": exc.kind})
            if exc.kind in FATAL_KINDS:
                raise
            return {}
        self._calls.append(
            {
                "role": role,
                "model": result.model,
                "latency_s": result.latency_s,
                "input_tokens": result.usage.input_tokens
                + result.usage.cache_read_input_tokens
                + result.usage.cache_creation_input_tokens,
                "output_tokens": result.usage.output_tokens,
                "cost_usd": result.cost_usd,
                "fallback_used": result.fallback_used,
                "error": None,
            }
        )
        return result.data

    def owner_turn(self, request: Any) -> dict[str, Any]:
        return self._call(
            "owner", self.owner_system, _as_json(owner_payload(request)), OWNER_SCHEMA
        )

    def deliver(self, original: str) -> dict[str, Any]:
        """Отдельный вызов агента получателя только по оригиналу."""
        return self._call(
            "recipient",
            self.recipient_system,
            _as_json(recipient_payload(original)),
            RECIPIENT_SCHEMA,
        )

    # ------------------------------------------------------------------ интерфейс EvalModel

    def respond(self, request: Any) -> Any:
        response_cls = sys.modules[type(request).__module__].EvalResponse
        owner = self.owner_turn(request)
        pm = request.partner_message
        recipient = self.deliver(pm.original) if pm is not None else {}

        tone = delivery = adapted = None
        flagged = False
        if pm is not None and recipient:
            tone = int(recipient["tone"])
            delivery, adapted = decide_delivery(pm.original, recipient)
            flagged = recipient.get("safety", "none") != "none"
        if not owner:  # агент владельца не ответил — ситуация не проходит, без выдумок
            return response_cls(
                level=0,
                strategy="model_error",
                reply="",
                tone=tone,
                delivery=delivery,
                adapted=adapted,
                safety_event=flagged,
            )
        crisis_type = owner.get("crisis_type")
        return response_cls(
            level=int(owner["level"]),
            strategy=str(owner["strategy"]),
            reply=str(owner["reply"]),
            draft=owner.get("draft") or None,
            crisis_type=crisis_type,
            crisis_actions=frozenset(owner.get("crisis_actions", [])),
            safety_event=crisis_type is not None or flagged,
            partner_notified=False,
            tone=tone,
            delivery=delivery,
            adapted=adapted,
        )

    def pop_metrics(self) -> dict[str, Any]:
        calls, self._calls = self._calls, []
        ok = [c for c in calls if c["error"] is None]
        return {
            "prompt": PROMPT_VERSION,
            "calls": calls,
            "latency_s": round(sum(c["latency_s"] for c in ok), 3),
            "input_tokens": sum(c["input_tokens"] for c in ok),
            "output_tokens": sum(c["output_tokens"] for c in ok),
            "cost_usd": round(sum(c["cost_usd"] for c in ok), 8),
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

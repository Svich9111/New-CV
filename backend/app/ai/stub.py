"""Заглушка поставщика модели — без сети (задача 0.5): тесты и ``demo --offline``.

``ScriptedProvider`` отвечает функцией-обработчиком и записывает запросы (для тестов изоляции:
что именно ушло бы в модель). ``demo_handler`` — детерминированные ответы ролей для сценария
«Анна и Игорь»; это не оценка качества. Роль узнаётся по полям схемы ответа."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.ai.llm import ChatMessage, StructuredResult, Usage

Handler = Callable[[str, dict[str, Any], str], dict[str, Any]]


def role_of(schema: Mapping[str, Any]) -> str:
    """Роль по полям схемы ответа (схемы ролей различаются набором полей)."""
    props = set(schema.get("properties", {}))
    for role, key in (
        ("dialog", "draft_ready"),
        ("draft", "need"),
        ("safety", "category"),
        ("tone", "signals"),
        ("adapt", "adapted"),
        ("meaning", "problems"),
        ("summary", "summary"),
        ("memory_extract", "items"),
        ("memory_merge", "same"),
        ("profile_summary", "profile"),
    ):
        if key in props:
            return role
    raise ValueError("неизвестная схема ответа")


@dataclass
class ScriptedProvider:
    """``handler(role, payload, system) -> data``; ``requests`` — что ушло бы в модель."""

    handler: Handler
    model: str = "stub"
    requests: list[dict[str, Any]] = field(default_factory=list)

    async def complete_json(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        schema: Mapping[str, Any],
        max_tokens: int = 16000,
    ) -> StructuredResult:
        role = role_of(schema)
        payload = json.loads(messages[-1].content)
        self.requests.append(
            {
                "role": role,
                "system": system,
                "messages": [(m.role, m.content) for m in messages],
                "max_tokens": max_tokens,
                "schema": dict(schema),
            }
        )
        data = self.handler(role, payload, system)
        return StructuredResult(
            data=data, model=self.model, usage=Usage(), latency_s=0.0, cost_usd=0.0
        )

    async def stream_text(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 16000,
    ) -> AsyncIterator[str]:
        raise NotImplementedError
        yield ""  # pragma: no cover


DEMO_DRAFT = (
    "Мне грустно, когда наши планы отменяются. Мне важно проводить с тобой время. Пожалуйста, "
    "предупреждай меня заранее, если планы меняются."
)


def demo_handler(role: str, payload: dict[str, Any], system: str) -> dict[str, Any]:
    """Ответы ролей для офлайн-сценария «Анна и Игорь» (выдуманные данные)."""
    if role == "dialog":
        partner = payload["partner_name"]
        owner_turns = [t for t in payload["dialog"] if t["from"] == "owner"]
        if len(owner_turns) == 1:
            return {
                "level": 2,
                "reply": (
                    "Слышу, как тебе сейчас обидно и одиноко. Расскажи, что случилось — что "
                    f"именно задело сильнее всего в том, как поступил {partner}?"
                ),
                "draft_ready": False,
                "crisis_type": None,
                "crisis_secondary": [],
            }
        return {
            "level": 2,
            "reply": (
                "Понимаю: обидно не только то, что вечер сорвался, а то, что ты узнаёшь об этом "
                "в последний момент. Похоже, тебе важно чувствовать, что ваше время ценно. "
                "Я предложу вариант сообщения — поправь его, как тебе ближе."
            ),
            "draft_ready": True,
            "crisis_type": None,
            "crisis_secondary": [],
        }
    if role == "draft":
        return {
            "feeling": "Мне грустно",
            "situation": "наши планы отменяются",
            "need": "проводить с тобой время",
            "request": "предупреждай меня заранее, если планы меняются",
            "text": DEMO_DRAFT,
        }
    if role == "safety":
        text = payload["text"].lower()
        threat = any(w in text for w in ("пожалеешь", "мало не покажется"))
        return {"category": "threat_or_insult" if threat else "none", "secondary": []}
    if role == "tone":
        text = payload["original"]
        harsh = "!" in text or "третий раз" in text
        return {"tone": 2 if harsh else 0, "signals": ["counting", "exclamation"] if harsh else []}
    if role == "adapt":
        text = payload["original"].replace("Но ты уже", "Ты уже")
        return {
            "adapted": text.replace("!", "."),
            "reply_hint": (
                "Похоже, автору важно проводить время вместе и знать о переменах заранее. "
                "Можно начать с того, что это услышано, и договориться, как предупреждать."
            ),
        }
    if role == "meaning":
        return {"problems": []}
    raise ValueError(role)


# --------------------------------------------------------------------------- поток (задача 2.1)

StreamScript = Callable[[dict[str, Any]], Sequence["str | BaseException"]]


def json_chunks(data: Mapping[str, Any], size: int = 7) -> list[str]:
    """JSON-ответ, порезанный на куски по ``size`` символов (как его отдаёт поток модели)."""
    raw = json.dumps(data, ensure_ascii=False)
    return [raw[i : i + size] for i in range(0, len(raw), size)]


@dataclass
class ScriptedStreamProvider(ScriptedProvider):
    """Заглушка с потоком: ``complete_json`` — как у ``ScriptedProvider`` (классификатор),
    ``stream_text`` — куски из ``stream(payload)``; исключение в списке бросается на своём
    месте. ``first_delay_s`` / ``chunk_delay_s`` — задержки (проверка «первый кусок не ждёт
    конца ответа»); ``stream_requests`` — что ушло бы в модель потоком."""

    stream: StreamScript | None = None
    first_delay_s: float = 0.0
    chunk_delay_s: float = 0.0
    stream_requests: list[dict[str, Any]] = field(default_factory=list)

    async def stream_text(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 16000,
    ) -> AsyncIterator[str]:
        payload = json.loads(messages[-1].content)
        self.stream_requests.append(
            {
                "system": system,
                "messages": [(m.role, m.content) for m in messages],
                "max_tokens": max_tokens,
            }
        )
        if self.stream is None:
            raise NotImplementedError
        if self.first_delay_s:
            await asyncio.sleep(self.first_delay_s)
        for i, item in enumerate(self.stream(payload)):
            if isinstance(item, BaseException):
                raise item
            if i and self.chunk_delay_s:
                await asyncio.sleep(self.chunk_delay_s)
            yield item

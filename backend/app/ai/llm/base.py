"""Интерфейс поставщика языковой модели (ТЗ §10: слой абстракции с возможностью смены поставщика).

Код продукта и адаптер эталонного набора работают только с ``LLMProvider``; SDK конкретного
поставщика импортируется только в его реализации (``claude.py``).

В запрос модели попадает только то, что передал вызывающий: системный промпт и сообщения.
Слой не добавляет идентификаторов пользователя (ТЗ §9.1 п.4: без идентификаторов Telegram)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Role = Literal["user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass(frozen=True)
class StructuredResult:
    """Разобранный JSON по схеме и служебные метрики вызова (без текста запроса)."""

    data: dict[str, Any]
    model: str  # какая модель фактически ответила (с учётом резервной)
    usage: Usage
    latency_s: float
    cost_usd: float
    fallback_used: bool = False
    attempts: tuple[str, ...] = field(default_factory=tuple)  # модели, которые пробовали


@runtime_checkable
class LLMProvider(Protocol):
    """Поставщик модели. Реализации: ``ClaudeProvider``, ``FallbackProvider``, тестовые."""

    @property
    def model(self) -> str: ...

    async def complete_json(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        schema: Mapping[str, Any],
        max_tokens: int = 16000,
    ) -> StructuredResult:
        """Ответ строго по JSON Schema. Ошибки — подклассы ``LLMError``."""
        ...

    def stream_text(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 16000,
    ) -> AsyncIterator[str]:
        """Потоковый текстовый ответ (для чата, задача 0.5+). Ошибки — подклассы ``LLMError``."""
        ...

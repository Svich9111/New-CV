"""Слой абстракции над языковой моделью (задача 0.4, ТЗ §10).

Работа с моделью в продукте и в эталонном наборе — только через этот пакет."""

from app.ai.llm.base import ChatMessage, LLMProvider, StructuredResult, Usage
from app.ai.llm.claude import HAIKU, OPUS, SONNET, ClaudeProvider
from app.ai.llm.errors import (
    LLMAuthError,
    LLMBadOutput,
    LLMConfigError,
    LLMError,
    LLMModelNotFound,
    LLMRefusal,
    LLMRequestError,
    LLMUnavailable,
)
from app.ai.llm.fallback import FallbackProvider
from app.ai.llm.pricing import PRICES, cost_usd

__all__ = [
    "HAIKU",
    "OPUS",
    "PRICES",
    "SONNET",
    "ChatMessage",
    "ClaudeProvider",
    "FallbackProvider",
    "LLMAuthError",
    "LLMBadOutput",
    "LLMConfigError",
    "LLMError",
    "LLMModelNotFound",
    "LLMProvider",
    "LLMRefusal",
    "LLMRequestError",
    "LLMUnavailable",
    "StructuredResult",
    "Usage",
    "cost_usd",
]

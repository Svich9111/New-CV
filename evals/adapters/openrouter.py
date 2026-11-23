"""Адаптер OpenRouter к эталонному набору (задача 0.4b, АР-22).

Запуск: ``--model adapters.openrouter:anthropic/claude-haiku-5.5`` (run.py) или
``--models adapters.openrouter:openai/gpt-5.6-terra …`` (compare.py): имя после двоеточия —
идентификатор модели OpenRouter; модуль отдаёт фабрику адаптера по любому имени с «/».

Логика шагов — та же, что у ``adapters.claude`` (общий класс ``ClaudeAdapter``): агент владельца
видит только данные владельца и одобренное сообщение пары; ``deliver`` — отдельный вызов
**только по оригиналу**; ``converse`` — без оригинала (run.py передаёт запрос без
``partner_message``). Работа с моделью — только через ``backend/app/ai/llm/``.

Перед прогоном адаптер берёт прайс модели из каталога OpenRouter (``/api/v1/models``): он
нужен, если в ответе нет ``usage.cost``; модели нет в каталоге — прогон не начинается.
Некорректный запрос (400: модель не поддерживает строгую схему и т. п.) — фатальная ошибка
только этой модели: compare.py фиксирует её и переходит к следующей.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.ai.llm import LLMProvider, OpenRouterProvider, Price, fetch_price

from adapters.claude import ClaudeAdapter


class OpenRouterAdapter(ClaudeAdapter):
    model_specific_fatal = frozenset({"bad_request"})

    def __init__(
        self,
        model_id: str,
        provider: LLMProvider | None = None,
        *,
        price: Price | None = None,
    ) -> None:
        self.model_id = model_id
        self.name = model_id
        if provider is None:
            # Ключ проверяется до запроса каталога: без OPENROUTER_API_KEY — LLMConfigError.
            OpenRouterProvider(model_id)
            if price is None:
                price = asyncio.run(fetch_price(model_id))
            provider = OpenRouterProvider(model_id, price=price)
        super().__init__(provider=provider)


def adapter_factory(model_id: str) -> Callable[[], OpenRouterAdapter]:
    def factory() -> OpenRouterAdapter:
        return OpenRouterAdapter(model_id)

    factory.__name__ = model_id
    return factory


def __getattr__(name: str) -> Any:
    """``adapters.openrouter:<поставщик>/<модель>`` → фабрика адаптера (PEP 562)."""
    if "/" in name:
        return adapter_factory(name)
    raise AttributeError(name)

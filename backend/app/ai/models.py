"""Модели по ролям агента (задача 0.5; гипотеза АР-19 — окончательный выбор после психолога).

Диалог, черновик и адаптация — Sonnet 5.5; классификаторы безопасности, тона и смысла —
Haiku 5.5. Работа с моделью — только через слой ``app.ai.llm`` (ТЗ §10)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.ai.llm import FallbackProvider, LLMProvider, OpenRouterProvider, fetch_price

# Идентификаторы OpenRouter — как в сравнении моделей 0.4b (АР-22).
OPENROUTER_SONNET = "anthropic/claude-sonnet-5.5"
OPENROUTER_HAIKU = "anthropic/claude-haiku-5.5"


@dataclass(frozen=True)
class AgentModels:
    """Поставщик модели для каждой роли."""

    dialog: LLMProvider
    draft: LLMProvider
    adapt: LLMProvider
    safety: LLMProvider
    tone: LLMProvider
    meaning: LLMProvider

    @classmethod
    def single(cls, provider: LLMProvider) -> AgentModels:
        """Одна модель на все роли (тесты, заглушка)."""
        return cls(provider, provider, provider, provider, provider, provider)

    @classmethod
    def split(cls, writer: LLMProvider, classifier: LLMProvider) -> AgentModels:
        """Гипотеза АР-19: ``writer`` — диалог, черновик, адаптация; ``classifier`` —
        безопасность, тон, контроль смысла."""
        return cls(
            dialog=writer,
            draft=writer,
            adapt=writer,
            safety=classifier,
            tone=classifier,
            meaning=classifier,
        )


async def openrouter_models_async(
    writer: str = OPENROUTER_SONNET,
    classifier: str = OPENROUTER_HAIKU,
    *,
    with_fallback: bool = True,
) -> AgentModels:
    """Модели через OpenRouter (ключ — ``OPENROUTER_API_KEY``; без него — ``LLMConfigError`` до
    сетевого запроса). Прайс берётся из каталога: без цены расходы не считаются как $0.
    ``with_fallback`` — резерв на недоступность: писатель ↔ классификатор (отказ модели на
    резерв не уходит, АР-20). Асинхронный вариант — для сервера с запущенным циклом (3.2)."""
    OpenRouterProvider(writer)  # ключ проверяется до запроса каталога
    w_price, c_price = await asyncio.gather(fetch_price(writer), fetch_price(classifier))
    w = OpenRouterProvider(writer, price=w_price)
    c = OpenRouterProvider(classifier, price=c_price)
    if not with_fallback:
        return AgentModels.split(w, c)
    return AgentModels.split(FallbackProvider(w, c), FallbackProvider(c, w))


def openrouter_models(
    writer: str = OPENROUTER_SONNET,
    classifier: str = OPENROUTER_HAIKU,
    *,
    with_fallback: bool = True,
) -> AgentModels:
    """Синхронный вариант ``openrouter_models_async`` — для консоли и прогона набора (вне цикла
    событий; внутри запущенного цикла — ``RuntimeError``, нужен асинхронный вариант)."""
    return asyncio.run(openrouter_models_async(writer, classifier, with_fallback=with_fallback))

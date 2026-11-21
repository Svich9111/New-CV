"""Прайс моделей для учёта стоимости (ТЗ §10: «учёт стоимости на пару»).

Цены — $ за 1 млн токенов, прайс Anthropic на 2026-11 (первая сторона API). Кэш: запись —
×1,25 цены входа, чтение — ×0,1. Для оценки, а не для биллинга."""

from __future__ import annotations

from dataclasses import dataclass

from app.ai.llm.base import Usage


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float


PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(4.0, 20.0),
    "claude-sonnet-5-5": Price(2.0, 10.0),
    "claude-haiku-5-5": Price(0.10, 0.50),
}


def cost_usd(model: str, usage: Usage) -> float | None:
    """Стоимость вызова; для модели без прайса — ``None`` («цены нет»), а не $0 (ТЗ §10)."""
    price = PRICES.get(model)
    if price is None:
        return None
    tokens_in = (
        usage.input_tokens
        + usage.cache_creation_input_tokens * 1.25
        + usage.cache_read_input_tokens * 0.1
    )
    return round(
        (tokens_in * price.input_per_mtok + usage.output_tokens * price.output_per_mtok) / 1e6, 8
    )

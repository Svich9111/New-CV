"""Резервная модель (ТЗ §10: «Предусмотрена резервная модель»).

``FallbackProvider`` пробует поставщиков по порядку. На следующего переходит только при ошибке с
``fallback_ok`` (недоступность, в т. ч. 408/429/5xx/сеть, испорченный ответ, модель не найдена).
Отказ модели по безопасности (``refusal``) на резервную модель не уходит (DECISIONS.md АР-20).
Ошибки настройки, ключа, лимита расходов и некорректного запроса возвращаются сразу: другая
модель их не исправит.
Если не ответил никто — последняя ошибка; её ``user_message`` показывается пользователю, а
черновик остаётся у вызывающего кода."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import replace
from typing import Any

from app.ai.llm.base import ChatMessage, LLMProvider, StructuredResult
from app.ai.llm.errors import LLMError


class FallbackProvider:
    def __init__(self, primary: LLMProvider, *fallbacks: LLMProvider) -> None:
        if not fallbacks:
            raise ValueError("нужна хотя бы одна резервная модель")
        self._chain: tuple[LLMProvider, ...] = (primary, *fallbacks)

    @property
    def model(self) -> str:
        return self._chain[0].model

    async def complete_json(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        schema: Mapping[str, Any],
        max_tokens: int = 16000,
    ) -> StructuredResult:
        tried: list[str] = []
        last: LLMError | None = None
        for provider in self._chain:
            tried.append(provider.model)
            try:
                result = await provider.complete_json(
                    system=system, messages=messages, schema=schema, max_tokens=max_tokens
                )
            except LLMError as exc:
                if not exc.fallback_ok:
                    raise
                last = exc
                continue
            return replace(result, fallback_used=len(tried) > 1, attempts=tuple(tried))
        raise last or LLMError("нет поставщиков")

    async def stream_text(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 16000,
    ) -> AsyncIterator[str]:
        """Переключение — только до первого фрагмента: начатый ответ не склеивается из двух
        моделей, ошибка после начала передаётся вызывающему."""
        async for _, chunk in self.stream_chunks(
            system=system, messages=messages, max_tokens=max_tokens
        ):
            yield chunk

    async def stream_chunks(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 16000,
    ) -> AsyncIterator[tuple[str, str]]:
        """То же, что ``stream_text``, но с моделью, которая отдала фрагмент (учёт резерва в
        записи вызова, задача 2.1). Правила переключения — те же."""
        last: LLMError | None = None
        for provider in self._chain:
            started = False
            try:
                async for chunk in provider.stream_text(
                    system=system, messages=messages, max_tokens=max_tokens
                ):
                    started = True
                    yield provider.model, chunk
                return
            except LLMError as exc:
                if started or not exc.fallback_ok:
                    raise
                last = exc
        raise last or LLMError("нет поставщиков")

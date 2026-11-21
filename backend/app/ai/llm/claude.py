"""Реализация ``LLMProvider`` для Claude API (официальный SDK ``anthropic``).

- Ключ — только из окружения ``ANTHROPIC_API_KEY`` (SDK читает его сам); без ключа —
  ``LLMConfigError`` до любого сетевого запроса.
- Адаптивное мышление (``thinking: adaptive``); ``temperature``/``top_p`` не передаются.
- Структурированный ответ — ``output_config.format`` с JSON Schema.
- ``stop_reason`` проверяется до чтения ``content`` (в т. ч. ``refusal``).
- Ошибки SDK переводятся в типизированные ``LLMError`` цепочкой от частных к общим.
- В запрос не добавляются ``metadata``/идентификаторы пользователя (ТЗ §9.1 п.4).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Literal, NoReturn

import anthropic
from anthropic.types import (
    AssistantMessageParam,
    Message,
    MessageParam,
    OutputConfigParam,
    UserMessageParam,
)

from app.ai.llm.base import ChatMessage, StructuredResult, Usage
from app.ai.llm.errors import (
    LLMAuthError,
    LLMBadOutput,
    LLMConfigError,
    LLMModelNotFound,
    LLMRefusal,
    LLMRequestError,
    LLMUnavailable,
)
from app.ai.llm.pricing import cost_usd

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"
API_KEY_ENV = "ANTHROPIC_API_KEY"

Effort = Literal["low", "medium", "high", "xhigh", "max"]


def _client_from_env() -> anthropic.AsyncAnthropic:
    if not os.environ.get(API_KEY_ENV):
        raise LLMConfigError(
            f"Не задан {API_KEY_ENV}: ключ Claude API берётся только из переменной окружения"
        )
    return anthropic.AsyncAnthropic()


def _translate(exc: Exception, model: str) -> NoReturn:
    """Ошибка SDK → ``LLMError``. Порядок важен: подклассы раньше базовых."""
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        raise LLMAuthError(f"ключ отклонён ({exc.status_code})", model=model) from exc
    if isinstance(exc, anthropic.NotFoundError):
        raise LLMModelNotFound(f"модель {model} не найдена (404)", model=model) from exc
    if isinstance(exc, anthropic.RateLimitError):
        raise LLMUnavailable("лимит запросов (429)", model=model) from exc
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500:
            raise LLMUnavailable(f"ошибка поставщика ({exc.status_code})", model=model) from exc
        raise LLMRequestError(f"запрос отклонён ({exc.status_code})", model=model) from exc
    if isinstance(exc, anthropic.APIConnectionError):  # включает APITimeoutError
        raise LLMUnavailable("нет связи с поставщиком", model=model) from exc
    raise exc


def _usage(message: Message) -> Usage:
    u = message.usage
    return Usage(
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cache_read_input_tokens=u.cache_read_input_tokens or 0,
        cache_creation_input_tokens=u.cache_creation_input_tokens or 0,
    )


def _check_stop(message: Message, model: str) -> None:
    if message.stop_reason == "refusal":
        details = message.stop_details
        category = getattr(details, "category", None) if details else None
        raise LLMRefusal("модель отказалась отвечать", model=model, category=category)
    if message.stop_reason in ("max_tokens", "model_context_window_exceeded"):
        raise LLMBadOutput(f"ответ обрезан ({message.stop_reason})", model=model)


class ClaudeProvider:
    """Один идентификатор модели Claude. Резервная модель — ``FallbackProvider``."""

    def __init__(
        self,
        model: str,
        *,
        client: anthropic.AsyncAnthropic | None = None,
        effort: Effort | None = None,
    ) -> None:
        self._model = model
        self._client = client if client is not None else _client_from_env()
        self._effort = effort

    @property
    def model(self) -> str:
        return self._model

    def _params(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        max_tokens: int,
        schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        api_messages: list[MessageParam] = [
            UserMessageParam(role="user", content=m.content)
            if m.role == "user"
            else AssistantMessageParam(role="assistant", content=m.content)
            for m in messages
        ]
        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": api_messages,
            "thinking": {"type": "adaptive"},
        }
        config: OutputConfigParam = {}
        if schema is not None:
            config["format"] = {"type": "json_schema", "schema": dict(schema)}
        if self._effort is not None:
            config["effort"] = self._effort
        if config:
            params["output_config"] = config
        return params

    async def complete_json(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        schema: Mapping[str, Any],
        max_tokens: int = 16000,
    ) -> StructuredResult:
        started = time.monotonic()
        try:
            message = await self._client.messages.create(
                **self._params(system, messages, max_tokens, schema)
            )
        except anthropic.APIError as exc:
            _translate(exc, self._model)
        latency = time.monotonic() - started
        _check_stop(message, self._model)
        text = next((b.text for b in message.content if b.type == "text"), None)
        if text is None:
            raise LLMBadOutput("в ответе нет текстового блока", model=self._model)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMBadOutput("ответ не разбирается как JSON", model=self._model) from exc
        if not isinstance(data, dict):
            raise LLMBadOutput("ответ — не JSON-объект", model=self._model)
        usage = _usage(message)
        return StructuredResult(
            data=data,
            model=message.model or self._model,
            usage=usage,
            latency_s=round(latency, 3),
            cost_usd=cost_usd(self._model, usage),
            attempts=(self._model,),
        )

    async def stream_text(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 16000,
    ) -> AsyncIterator[str]:
        try:
            async with self._client.messages.stream(
                **self._params(system, messages, max_tokens)
            ) as stream:
                async for chunk in stream.text_stream:
                    yield chunk
                final = await stream.get_final_message()
        except anthropic.APIError as exc:
            _translate(exc, self._model)
        _check_stop(final, self._model)

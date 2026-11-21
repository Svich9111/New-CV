"""Реализация ``LLMProvider`` для Claude API (официальный SDK ``anthropic``).

- Ключ — только из окружения ``ANTHROPIC_API_KEY`` (SDK читает его сам); без ключа —
  ``LLMConfigError`` до любого сетевого запроса. Модель без цены в ``pricing.py`` — тоже
  ``LLMConfigError``: расходы не должны считаться как $0 (ТЗ §10).
- Адаптивное мышление (``thinking: adaptive``); ``temperature``/``top_p`` не передаются.
- Структурированный ответ — ``output_config.format`` с JSON Schema.
- ``stop_reason`` проверяется до чтения ``content`` (в т. ч. ``refusal``).
- Ошибки SDK переводятся в типизированные ``LLMError`` цепочкой от частных к общим. Ошибка
  после полученного ответа (отказ, обрезанный или испорченный ответ) несёт оплаченные
  ``usage`` и ``cost_usd``.
- В запрос не добавляются ``metadata``/идентификаторы пользователя (ТЗ §9.1 п.4).
- На DEBUG SDK пишет тело запроса с текстами — фильтр ``RedactRequestBody`` его вырезает
  (CLAUDE.md: «в журналах нет текстов сообщений»).
"""

from __future__ import annotations

import json
import logging
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
    LLMError,
    LLMModelNotFound,
    LLMQuotaError,
    LLMRefusal,
    LLMRequestError,
    LLMUnavailable,
)
from app.ai.llm.pricing import PRICES, cost_usd

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"
API_KEY_ENV = "ANTHROPIC_API_KEY"

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class RedactRequestBody(logging.Filter):
    """На DEBUG SDK пишет «Request options: {… json_data: …}» — тело запроса с репликами и
    памятью. Фильтр заменяет такую запись служебной строкой; остальные записи SDK (метод, адрес,
    статус) остаются (A8 D-2, CLAUDE.md: «в журналах нет текстов сообщений»)."""

    def filter(self, record: logging.LogRecord) -> bool:
        text = str(record.msg)
        if text.startswith("Request options") or "json_data" in repr(record.args):
            record.msg = "Request options: <тело запроса не журналируется>"
            record.args = ()
        return True


_REDACT = RedactRequestBody()


def redact_sdk_logs() -> None:
    """Фильтр на журнал, который пишет параметры запроса (и на корневой журнал SDK)."""
    for name in ("anthropic._base_client", "anthropic"):
        logger = logging.getLogger(name)
        if _REDACT not in logger.filters:
            logger.addFilter(_REDACT)


redact_sdk_logs()

# Исчерпан лимит расходов или нет оплаты: резервная модель того же аккаунта не поможет.
_SPEND_LIMIT = (
    anthropic.PaymentRequiredError,  # 402, в т. ч. LinkedOrganization*
    anthropic.ContractSpendLimitReachedError,
    anthropic.EnforcedSpendLimitReachedError,
)


def _client_from_env() -> anthropic.AsyncAnthropic:
    if not os.environ.get(API_KEY_ENV):
        raise LLMConfigError(
            f"Не задан {API_KEY_ENV}: ключ Claude API берётся только из переменной окружения"
        )
    return anthropic.AsyncAnthropic()


def _translate(exc: anthropic.APIError, model: str) -> NoReturn:
    """Ошибка SDK → ``LLMError``. Порядок важен: подклассы раньше базовых."""
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        raise LLMAuthError(f"ключ отклонён ({exc.status_code})", model=model) from exc
    if isinstance(exc, anthropic.NotFoundError):
        raise LLMModelNotFound(f"модель {model} не найдена (404)", model=model) from exc
    if isinstance(exc, _SPEND_LIMIT):
        raise LLMQuotaError(f"лимит расходов или оплата ({exc.status_code})", model=model) from exc
    if isinstance(exc, anthropic.RateLimitError):
        raise LLMUnavailable("лимит запросов (429)", model=model) from exc
    if isinstance(exc, anthropic.RequestTimeoutError):  # 408 — временная недоступность
        raise LLMUnavailable("тайм-аут на стороне поставщика (408)", model=model) from exc
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500:
            raise LLMUnavailable(f"ошибка поставщика ({exc.status_code})", model=model) from exc
        raise LLMRequestError(f"запрос отклонён ({exc.status_code})", model=model) from exc
    if isinstance(exc, anthropic.APIConnectionError):  # включает APITimeoutError
        raise LLMUnavailable("нет связи с поставщиком", model=model) from exc
    if isinstance(exc, anthropic.APIResponseValidationError):
        raise LLMBadOutput("ответ поставщика не прошёл проверку SDK", model=model) from exc
    raise LLMUnavailable(f"ошибка SDK ({type(exc).__name__})", model=model) from exc


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
        if model not in PRICES:
            raise LLMConfigError(f"Нет цены для модели {model} в app/ai/llm/pricing.py")
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

    def _cost(self, usage: Usage) -> float:
        cost = cost_usd(self._model, usage)
        if cost is None:  # не бывает: цена проверена в конструкторе
            raise LLMConfigError(f"Нет цены для модели {self._model}", model=self._model)
        return cost

    def _parse(self, message: Message) -> dict[str, Any]:
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
        return data

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
        usage = _usage(message)
        try:
            data = self._parse(message)
        except LLMError as exc:  # ответ получен и оплачен — расход не теряется (MINOR-4)
            exc.usage, exc.cost_usd = usage, self._cost(usage)
            raise
        return StructuredResult(
            data=data,
            model=message.model or self._model,
            usage=usage,
            latency_s=round(latency, 3),
            cost_usd=self._cost(usage),
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

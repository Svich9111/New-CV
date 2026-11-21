"""Реализация ``LLMProvider`` для OpenRouter (задача 0.4b, DECISIONS.md АР-22).

OpenRouter — HTTP API, совместимое с OpenAI chat completions: один ключ — модели разных
поставщиков. Используется для предварительного сравнения моделей на выдуманном эталонном наборе.

- Ключ — только из окружения ``OPENROUTER_API_KEY``; без него — ``LLMConfigError`` до сетевого
  запроса. В облачном окружении заголовок подставляет прокси (значение переменной — заглушка).
- В каждом запросе ``provider.data_collection: "deny"`` (поставщик не хранит и не обучается на
  данных, ТЗ §9.1 п.4) и ``require_parameters: true`` (маршрут только к поставщику, который
  поддерживает строгую схему ответа).
- Структурированный ответ — ``response_format`` json_schema со ``strict: true``.
- ``temperature``/``top_p``/``reasoning`` не передаются: параметры модели по умолчанию.
- В запрос не добавляются ``user``, ``metadata``, заголовки ``HTTP-Referer``/``X-Title`` и
  другие идентификаторы (ТЗ §9.1 п.4).
- Стоимость — ``usage.cost`` ответа (``usage.include: true``); если её нет — по прайсу,
  переданному в конструктор (``/api/v1/models``); если нет и прайса — ``LLMConfigError``:
  расходы не должны считаться как $0 (ТЗ §10).
- Ошибки: 401/403 — ``LLMAuthError``; 402 — ``LLMQuotaError`` (остановка, без резерва);
  404 — ``LLMModelNotFound``; 408/429/5xx/сеть — ``LLMUnavailable`` (с повтором);
  прочие 4xx — ``LLMRequestError``; отказ модели — ``LLMRefusal`` (без резерва, АР-20);
  обрезанный/пустой/невалидный ответ — ``LLMBadOutput``.
- Тексты запросов и ответов не журналируются и в тексты ошибок не попадают (CLAUDE.md).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import httpx

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
from app.ai.llm.pricing import Price

API_KEY_ENV = "OPENROUTER_API_KEY"
BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT_S = 600.0
DEFAULT_RETRIES = 2  # только для временной недоступности (408/429/5xx/сеть)
_DETAIL_LIMIT = 160


def _api_key() -> str:
    key = os.environ.get(API_KEY_ENV)
    if not key:
        raise LLMConfigError(
            f"Не задан {API_KEY_ENV}: ключ OpenRouter берётся только из переменной окружения"
        )
    return key


def _error_message(body: Any) -> str:
    """Короткое описание ошибки сервиса (без ``metadata``/``user_id`` и сырых ответов)."""
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        message = body["error"].get("message")
        if isinstance(message, str):
            return message[:_DETAIL_LIMIT]
    return ""


def _status_error(status: int, body: Any, model: str) -> LLMError:
    note = _error_message(body)
    detail = f"({status}) {note}".strip()
    if status in (401, 403):
        return LLMAuthError(f"ключ отклонён {detail}", model=model)
    if status == 402:
        return LLMQuotaError(f"лимит расходов или оплата {detail}", model=model)
    if status == 404:
        return LLMModelNotFound(f"модель {model} недоступна {detail}", model=model)
    if status in (408, 429) or status >= 500:
        return LLMUnavailable(f"временная недоступность {detail}", model=model)
    return LLMRequestError(f"запрос отклонён {detail}", model=model)


def _usage(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    details = raw.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    written = int(details.get("cache_write_tokens") or 0)
    prompt = int(raw.get("prompt_tokens") or 0)
    return Usage(
        input_tokens=max(prompt - cached - written, 0),
        output_tokens=int(raw.get("completion_tokens") or 0),
        cache_read_input_tokens=cached,
        cache_creation_input_tokens=written,
    )


class OpenRouterProvider:
    """Одна модель OpenRouter (``anthropic/claude-haiku-5.5`` и т. п.)."""

    def __init__(
        self,
        model: str,
        *,
        client: httpx.AsyncClient | None = None,
        price: Price | None = None,
        retries: int = DEFAULT_RETRIES,
        retry_delay_s: float = 2.0,
    ) -> None:
        self._model = model
        self._key = _api_key()
        self._client = client
        self._price = price
        self._retries = retries
        self._retry_delay_s = retry_delay_s

    @property
    def model(self) -> str:
        return self._model

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}

    def request_body(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        max_tokens: int,
        schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Тело запроса: только системный промпт, сообщения и служебные параметры."""
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                *({"role": m.role, "content": m.content} for m in messages),
            ],
            "max_tokens": max_tokens,
            "provider": {"data_collection": "deny", "require_parameters": True},
            "usage": {"include": True},
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": True, "schema": dict(schema)},
            }
        return body

    def _cost(self, raw: Any, usage: Usage) -> float:
        cost = raw.get("cost") if isinstance(raw, dict) else None
        if isinstance(cost, int | float) and not isinstance(cost, bool):
            return round(float(cost), 8)
        if self._price is None:
            raise LLMConfigError(
                f"OpenRouter не вернул usage.cost, а прайса для {self._model} нет",
                model=self._model,
            )
        tokens_in = (
            usage.input_tokens
            + usage.cache_creation_input_tokens * 1.25
            + usage.cache_read_input_tokens * 0.1
        )
        return round(
            (
                tokens_in * self._price.input_per_mtok
                + usage.output_tokens * self._price.output_per_mtok
            )
            / 1e6,
            8,
        )

    async def _post_once(self, body: dict[str, Any]) -> Any:
        client = self._client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S)
        try:
            response = await client.post(
                f"{BASE_URL}/chat/completions", json=body, headers=self._headers()
            )
        except httpx.TimeoutException as exc:
            raise LLMUnavailable("тайм-аут запроса", model=self._model) from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailable(
                f"нет связи с поставщиком ({type(exc).__name__})", model=self._model
            ) from exc
        finally:
            if self._client is None:
                await client.aclose()
        try:
            data = json.loads(response.text)
        except ValueError:
            data = None
        if response.status_code != 200:
            raise _status_error(response.status_code, data, self._model)
        if not isinstance(data, dict):
            raise LLMBadOutput("ответ поставщика — не JSON", model=self._model)
        if isinstance(data.get("error"), dict):  # ошибка в теле ответа со статусом 200
            code = data["error"].get("code")
            status = code if isinstance(code, int) else 502
            raise _status_error(status, data, self._model)
        return data

    async def _post(self, body: dict[str, Any]) -> Any:
        attempt = 0
        while True:
            try:
                return await self._post_once(body)
            except LLMUnavailable:
                if attempt >= self._retries:
                    raise
                attempt += 1
                await asyncio.sleep(self._retry_delay_s * attempt)

    def _parse(self, data: dict[str, Any]) -> dict[str, Any]:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise LLMBadOutput("в ответе нет choices", model=self._model)
        choice = choices[0]
        if isinstance(choice.get("error"), dict):
            raise _status_error(502, {"error": choice["error"]}, self._model)
        message = choice.get("message") or {}
        finish = choice.get("finish_reason")
        native = choice.get("native_finish_reason")
        if message.get("refusal") or finish == "content_filter" or native == "refusal":
            raise LLMRefusal("модель отказалась отвечать", model=self._model, category=None)
        if finish == "length":
            raise LLMBadOutput("ответ обрезан (length)", model=self._model)
        if finish == "error":
            raise LLMUnavailable("поставщик прервал ответ (error)", model=self._model)
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise LLMBadOutput("пустой ответ", model=self._model)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMBadOutput("ответ не разбирается как JSON", model=self._model) from exc
        if not isinstance(parsed, dict):
            raise LLMBadOutput("ответ — не JSON-объект", model=self._model)
        return parsed

    async def complete_json(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        schema: Mapping[str, Any],
        max_tokens: int = 16000,
    ) -> StructuredResult:
        started = time.monotonic()
        data = await self._post(self.request_body(system, messages, max_tokens, schema))
        latency = time.monotonic() - started
        usage = _usage(data.get("usage"))
        try:
            parsed = self._parse(data)
        except LLMError as exc:  # ответ получен и оплачен — расход не теряется
            exc.usage = usage
            if data.get("usage") is not None:
                exc.cost_usd = self._cost(data.get("usage"), usage)
            raise
        cost = self._cost(data.get("usage"), usage)
        return StructuredResult(
            data=parsed,
            model=str(data.get("model") or self._model),
            usage=usage,
            latency_s=round(latency, 3),
            cost_usd=cost,
            attempts=(self._model,),
        )

    async def stream_text(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 16000,
    ) -> AsyncIterator[str]:
        """Без потока: для сравнения моделей не нужен; ответ отдаётся одним фрагментом."""
        data = await self._post(self.request_body(system, messages, max_tokens))
        choices = data.get("choices") or [{}]
        message = choices[0].get("message") or {}
        if message.get("refusal") or choices[0].get("native_finish_reason") == "refusal":
            raise LLMRefusal("модель отказалась отвечать", model=self._model)
        content = message.get("content")
        if not isinstance(content, str):
            raise LLMBadOutput("пустой ответ", model=self._model)
        yield content


async def fetch_price(model: str, client: httpx.AsyncClient | None = None) -> Price:
    """Прайс модели из ``/api/v1/models`` ($ за токен → $ за 1 млн токенов). Нет модели —
    ``LLMModelNotFound``: прогон не начинается с выдуманной ценой."""
    own = client or httpx.AsyncClient(timeout=60.0)
    try:
        response = await own.get(f"{BASE_URL}/models")
    except httpx.HTTPError as exc:
        raise LLMUnavailable("нет связи с OpenRouter (/models)", model=model) from exc
    finally:
        if client is None:
            await own.aclose()
    if response.status_code != 200:
        raise _status_error(response.status_code, None, model)
    for row in response.json().get("data", []):
        if row.get("id") == model:
            pricing = row.get("pricing") or {}
            return Price(
                float(pricing.get("prompt") or 0) * 1e6,
                float(pricing.get("completion") or 0) * 1e6,
            )
    raise LLMModelNotFound(f"модели {model} нет в каталоге OpenRouter", model=model)

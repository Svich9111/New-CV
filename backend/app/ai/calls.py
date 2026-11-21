"""Общий вызов модели для ролей агента (задача 0.5): JSON на входе, JSON по схеме на выходе.

- Вход модели — системный промпт роли и **одно** сообщение пользователя: JSON-объект с данными
  от сервера. Тексты людей (реплики, сообщение партнёра) — значения полей этого объекта, а не
  инструкции (ТЗ §9.2 п.3); ``json.dumps`` экранирует кавычки и переводы строк, поэтому текст
  не может «выйти» из своего поля.
- Ответ проверяется ещё раз в коде (``validate``): несоответствие — ``LLMBadOutput``, как у слоя.
- Ошибки — подклассы ``LLMError`` слоя ``app.ai.llm`` (у каждой есть ``user_message``).
- Учёт стоимости (ТЗ §10): каждый вызов — ``CallRecord`` без текстов (роль, промпт, модель,
  токены, стоимость, время, вид ошибки). Журналов с текстами модуль не пишет (CLAUDE.md)."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from app.ai.llm import ChatMessage, LLMBadOutput, LLMError, LLMProvider
from app.ai.prompts import Prompt


@dataclass(frozen=True)
class CallRecord:
    """Метрики одного вызова — без текстов запроса и ответа."""

    role: str
    prompt: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    fallback_used: bool = False
    error: str | None = None


@dataclass
class CallLog:
    """Вызовы одного действия (ответ в чате, обработка сообщения) — для учёта стоимости."""

    records: list[CallRecord] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return round(sum(r.cost_usd for r in self.records), 8)


BAD_OUTPUT_RETRIES = 1


def as_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def strict_object(properties: dict[str, Any]) -> dict[str, Any]:
    """Схема ответа: все поля обязательны, лишних нет (строгий режим поставщиков)."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


async def complete[T](
    provider: LLMProvider,
    *,
    prompt: Prompt,
    payload: Mapping[str, Any],
    schema: Mapping[str, Any],
    validate: Callable[[dict[str, Any]], T],
    log: CallLog,
    max_tokens: int,
    retries_bad_output: int = BAD_OUTPUT_RETRIES,
) -> T:
    """Один вызов роли. Ошибка модели или проверки — ``LLMError`` (записывается в ``log``).
    Испорченный или обрезанный ответ (``bad_output``) повторяется ``retries_bad_output`` раз:
    это сбой генерации, а не отказ; отказ (АР-20) и недоступность не повторяются здесь
    (недоступность повторяет поставщик, резерв — ``FallbackProvider``)."""
    attempt = 0
    while True:
        try:
            return await _complete_once(
                provider,
                prompt=prompt,
                payload=payload,
                schema=schema,
                validate=validate,
                log=log,
                max_tokens=max_tokens,
            )
        except LLMBadOutput:
            if attempt >= retries_bad_output:
                raise
            attempt += 1


async def _complete_once[T](
    provider: LLMProvider,
    *,
    prompt: Prompt,
    payload: Mapping[str, Any],
    schema: Mapping[str, Any],
    validate: Callable[[dict[str, Any]], T],
    log: CallLog,
    max_tokens: int,
) -> T:
    messages = [ChatMessage("user", as_json(payload))]
    try:
        result = await provider.complete_json(
            system=prompt.text, messages=messages, schema=schema, max_tokens=max_tokens
        )
    except LLMError as exc:
        usage = exc.usage
        log.records.append(
            CallRecord(
                role=prompt.role,
                prompt=prompt.name,
                model=exc.model or provider.model,
                input_tokens=_tokens_in(usage),
                output_tokens=usage.output_tokens if usage else 0,
                cost_usd=exc.cost_usd or 0.0,
                error=exc.kind,
            )
        )
        raise
    record = CallRecord(
        role=prompt.role,
        prompt=prompt.name,
        model=result.model,
        input_tokens=_tokens_in(result.usage),
        output_tokens=result.usage.output_tokens,
        cost_usd=result.cost_usd,
        latency_s=result.latency_s,
        fallback_used=result.fallback_used,
    )
    try:
        value = validate(result.data)
    except (KeyError, TypeError, ValueError) as exc:
        log.records.append(replace(record, error="bad_output"))
        raise LLMBadOutput(
            f"{prompt.name}: ответ не по схеме ({type(exc).__name__})", model=result.model
        ) from exc
    log.records.append(record)
    return value


def _tokens_in(usage: Any) -> int:
    if usage is None:
        return 0
    return int(
        usage.input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens
    )


# --------------------------------------------------------------------------- проверки ответа


def text_field(data: Mapping[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise TypeError(key)
    if not allow_empty and not value.strip():
        raise ValueError(key)
    return value.strip()


def optional_text(data: Mapping[str, Any], key: str) -> str | None:
    value = data[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(key)
    return value.strip() or None


def int_field(data: Mapping[str, Any], key: str, allowed: range) -> int:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int) or value not in allowed:
        raise ValueError(key)
    return value


def enum_list(data: Mapping[str, Any], key: str, allowed: tuple[str, ...]) -> tuple[str, ...]:
    value = data[key]
    if not isinstance(value, list) or any(v not in allowed for v in value):
        raise ValueError(key)
    return tuple(dict.fromkeys(value))

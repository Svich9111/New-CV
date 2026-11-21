"""Типизированные ошибки слоя модели (ТЗ §10: «понятное сообщение, черновик не теряется»).

Слой не хранит и не удаляет черновики: при ошибке вызывающий код оставляет черновик как был и
показывает пользователю ``user_message``. Текст сообщений пользователя в ошибки не попадает
(CLAUDE.md: в журналах нет текстов)."""

from __future__ import annotations

USER_MESSAGE_UNAVAILABLE = (
    "Агент сейчас не может ответить. Твой текст сохранён — попробуй ещё раз через минуту."
)
USER_MESSAGE_REFUSAL = (
    "Агент не смог обработать это сообщение. Текст сохранён — его можно изменить или отправить "
    "позже."
)


class LLMError(Exception):
    """Базовая ошибка слоя. ``fallback_ok`` — можно ли повторить запрос на резервной модели."""

    kind = "error"
    fallback_ok = False
    user_message = USER_MESSAGE_UNAVAILABLE

    def __init__(self, detail: str, *, model: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.model = model


class LLMConfigError(LLMError):
    """Слой не настроен: нет ключа в окружении и т. п. Повтор на другой модели не поможет."""

    kind = "config"


class LLMAuthError(LLMError):
    """Ключ отклонён поставщиком (401/403)."""

    kind = "auth"


class LLMRequestError(LLMError):
    """Запрос некорректен (400, 413, 422): ошибка в коде, а не в доступности модели."""

    kind = "bad_request"


class LLMModelNotFound(LLMError):
    """Модель с таким идентификатором недоступна (404) — пробуем резервную."""

    kind = "model_not_found"
    fallback_ok = True


class LLMUnavailable(LLMError):
    """Лимит запросов, перегрузка, 5xx, сеть, тайм-аут — временная недоступность."""

    kind = "unavailable"
    fallback_ok = True


class LLMRefusal(LLMError):
    """Модель отказалась отвечать (stop_reason = refusal)."""

    kind = "refusal"
    fallback_ok = True
    user_message = USER_MESSAGE_REFUSAL

    def __init__(
        self, detail: str, *, model: str | None = None, category: str | None = None
    ) -> None:
        super().__init__(detail, model=model)
        self.category = category


class LLMBadOutput(LLMError):
    """Ответ обрезан (max_tokens) или не разбирается как JSON по схеме."""

    kind = "bad_output"
    fallback_ok = True

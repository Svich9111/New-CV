"""POST /api/v1/telegram/webhook — служебный маршрут приёма обновлений Telegram.

Не входит в контракт клиента Mini App (api/openapi.yaml): его вызывает только Telegram, а
подлинность запроса подтверждает секретный заголовок `X-Telegram-Bot-Api-Secret-Token`
(задаётся в setWebhook, см. app/bot/setup.py). Сравнение — за постоянное время.

Ответы: 401 — нет заголовка, 403 — неверный, 503 — секрет не настроен или клиента Bot API нет,
временный сбой Bot API или превышен срок обработки (Telegram доставит обновление повторно),
повтор пришёл во время первой доставки, а та не завершилась успешно; иначе 200, в том числе
для неизвестных и неразборчивых обновлений, чтобы Telegram не повторял их бесконечно.

Обработка обновления — общая с опросом getUpdates (app/bot/processing.py)."""

import hmac
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Header, Request, Response

from app.api.deps import AppSettings
from app.bot.client import BotApi
from app.bot.dedup import UpdateDeduplicator
from app.bot.handlers import handle_update
from app.bot.processing import HANDLE_DEADLINE_S, Delivery, process_update
from app.errors import AppError

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105 — имя заголовка, не секрет
WEBHOOK_PATH = "/telegram/webhook"
# Срок обработки (HANDLE_DEADLINE_S) и обработчик (handle_update) — имена этого модуля, их
# читает маршрут при каждом вызове (тесты подменяют их здесь).
__all__ = ["HANDLE_DEADLINE_S", "WEBHOOK_PATH", "handle_update", "router"]

log = logging.getLogger("app.bot")
router = APIRouter()


def _unavailable() -> AppError:
    return AppError(503, "service_unavailable", "Сервис временно недоступен")


def _check_secret(header: str | None, expected: str) -> None:
    if not expected:
        # Пустой секрет совпал бы с пустым заголовком — без настройки не принимаем ничего.
        raise _unavailable()
    if header is None:
        raise AppError(401, "unauthorized", "Нет секретного заголовка")
    if not hmac.compare_digest(header.encode(), expected.encode()):
        raise AppError(403, "forbidden", "Неверный секретный заголовок")


@router.post(WEBHOOK_PATH, include_in_schema=False, status_code=200)
async def telegram_webhook(
    request: Request,
    settings: AppSettings,
    secret: Annotated[str | None, Header(alias=SECRET_HEADER)] = None,
) -> Response:
    _check_secret(secret, settings.telegram_webhook_secret.get_secret_value())
    api: BotApi | None = getattr(request.app.state, "bot_api", None)
    updates: UpdateDeduplicator = request.app.state.bot_updates
    if api is None:
        raise _unavailable()
    try:
        update = json.loads(await request.body())
    except Exception:
        # Любая ошибка разбора (ValueError, RecursionError на глубокой вложенности и т. п.) —
        # обновление неразборчиво, повтор бесполезен.
        log.warning("telegram update ignored: invalid json")
        return Response(status_code=200)
    delivery = await process_update(
        api,
        updates,
        update,
        settings.webapp_url,
        handler=handle_update,
        deadline_s=HANDLE_DEADLINE_S,
    )
    if delivery is Delivery.RETRY:
        raise _unavailable()
    return Response(status_code=200)

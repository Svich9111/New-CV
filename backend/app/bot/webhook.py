"""POST /api/v1/telegram/webhook — служебный маршрут приёма обновлений Telegram.

Не входит в контракт клиента Mini App (api/openapi.yaml): его вызывает только Telegram, а
подлинность запроса подтверждает секретный заголовок `X-Telegram-Bot-Api-Secret-Token`
(задаётся в setWebhook, см. app/bot/setup.py). Сравнение — за постоянное время.

Ответы: 401 — нет заголовка, 403 — неверный, 503 — секрет не настроен или клиента Bot API нет,
либо временный сбой Bot API (Telegram доставит обновление повторно); иначе 200, в том числе для
неизвестных и неразборчивых обновлений, чтобы Telegram не повторял их бесконечно."""

import hmac
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request, Response

from app.api.deps import AppSettings
from app.bot.client import BotApi, BotApiError
from app.bot.dedup import UpdateDeduplicator
from app.bot.handlers import handle_update, update_kind
from app.errors import AppError

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105 — имя заголовка, не секрет
WEBHOOK_PATH = "/telegram/webhook"

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


def _update_id(update: Any) -> int | None:
    if not isinstance(update, dict):
        return None
    uid = update.get("update_id")
    if not isinstance(uid, int) or isinstance(uid, bool):
        return None
    return uid


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
    except ValueError:
        log.warning("telegram update ignored: invalid json")
        return Response(status_code=200)
    update_id = _update_id(update)
    if update_id is None:
        log.warning("telegram update ignored: no update_id")
        return Response(status_code=200)
    if not updates.claim(update_id):
        log.info("telegram update %d duplicate", update_id)
        return Response(status_code=200)
    kind = update_kind(update)
    try:
        outcome = await handle_update(api, update, settings.webapp_url)
    except BotApiError as err:
        if err.transient:
            updates.release(update_id)  # повторная доставка будет обработана
            log.warning("telegram update %d %s: bot api transient failure", update_id, kind)
            raise _unavailable() from None
        log.warning("telegram update %d %s: bot api error %s", update_id, kind, err.error_code)
        return Response(status_code=200)
    except Exception:
        # Неожиданная ошибка обработчика не должна приводить к бесконечным повторам Telegram.
        # Трассировку не пишем: в локальных переменных и сообщениях может быть текст.
        log.error("telegram update %d %s: handler failed", update_id, kind)
        return Response(status_code=200)
    log.info("telegram update %d %s: %s", update_id, kind, outcome)
    return Response(status_code=200)

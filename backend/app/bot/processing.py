"""Общий путь обработки одного обновления Telegram — для вебхука (app/bot/webhook.py) и для
опроса getUpdates в режиме разработки (app/bot/poll.py).

Исход — подтвердить обновление (вебхук: 200; опрос: сдвинуть offset) или нет (вебхук: 503;
опрос: запросить то же обновление снова после паузы). Идемпотентность — по update_id
(`UpdateDeduplicator`), общий срок обработки — `HANDLE_DEADLINE_S`.

Семантика — «хотя бы один раз»: при тайм-ауте чтения Bot API сообщение могло уйти, а отметка
снимается, поэтому возможен второй ответ на /start (безвредно; уведомления 3.5 — Б-27).
В журнал попадают только update_id, вид обновления и метка исхода — без текстов и payload."""

import asyncio
import enum
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.bot.client import BotApi, BotApiError
from app.bot.dedup import UpdateDeduplicator
from app.bot.handlers import handle_update, update_kind

# Предел обработки одного обновления, секунд. Telegram ждёт ответа на вебхук порядка минуты;
# без предела повторы 429 (до 2×10 с) и тайм-ауты (до 3×15 с) могли бы его превысить.
HANDLE_DEADLINE_S = 20.0

Handler = Callable[[BotApi, dict[str, Any], str], Awaitable[str]]

log = logging.getLogger("app.bot")


class Delivery(enum.Enum):
    ACK = "ack"  # обработано (или обрабатывать бесполезно) — подтвердить
    RETRY = "retry"  # временный сбой — не подтверждать, обновление придёт снова


def update_id_of(update: Any) -> int | None:
    if not isinstance(update, dict):
        return None
    uid = update.get("update_id")
    if not isinstance(uid, int) or isinstance(uid, bool):
        return None
    return uid


async def process_update(
    api: BotApi,
    updates: UpdateDeduplicator,
    update: Any,
    webapp_url: str,
    *,
    handler: Handler = handle_update,
    deadline_s: float = HANDLE_DEADLINE_S,
) -> Delivery:
    """Обрабатывает разобранное обновление. Отмена (CancelledError) снимает отметку
    update_id и пробрасывается дальше."""
    update_id = update_id_of(update)
    if update_id is None:
        log.warning("telegram update ignored: no update_id")
        return Delivery.ACK
    if not updates.claim(update_id):
        # Если первая доставка ещё в работе, повтор ждёт её исхода и отвечает так же:
        # подтверждение — только когда она завершена; при её временном сбое — повтор.
        try:
            async with asyncio.timeout(deadline_s):
                completed = await updates.wait_outcome(update_id)
        except TimeoutError:
            completed = False
        if not completed:
            log.info("telegram update %d duplicate, first delivery not completed", update_id)
            return Delivery.RETRY
        log.info("telegram update %d duplicate", update_id)
        return Delivery.ACK
    kind = update_kind(update)
    try:
        # Общий срок обработки (повторы 429 + тайм-ауты Bot API) заметно меньше времени, которое
        # Telegram ждёт ответа на вебхук: ответ 503 успевает дойти, и доставка повторится.
        async with asyncio.timeout(deadline_s):
            outcome = await handler(api, update, webapp_url)
    except TimeoutError:
        updates.release(update_id)  # повторная доставка будет обработана
        log.warning("telegram update %d %s: deadline exceeded", update_id, kind)
        return Delivery.RETRY
    except BotApiError as err:
        if err.transient:
            updates.release(update_id)  # повторная доставка будет обработана
            log.warning("telegram update %d %s: bot api transient failure", update_id, kind)
            return Delivery.RETRY
        updates.complete(update_id)
        log.warning("telegram update %d %s: bot api error %s", update_id, kind, err.error_code)
        return Delivery.ACK
    except Exception:
        # Неожиданная ошибка обработчика не должна приводить к бесконечным повторам Telegram.
        # Трассировку не пишем: в локальных переменных и сообщениях может быть текст.
        updates.complete(update_id)
        log.error("telegram update %d %s: handler failed", update_id, kind)
        return Delivery.ACK
    except BaseException:
        # Отмена (asyncio.CancelledError — не Exception: клиент закрыл соединение, остановка
        # сервера или опроса) — исхода нет: снимаем отметку, иначе update_id навсегда
        # «в работе» и каждый повтор ждал бы срок (A8 1.4 D-3). Отмену не глушим.
        updates.release(update_id)
        log.warning("telegram update %d %s: handler cancelled", update_id, kind)
        raise
    updates.complete(update_id)
    log.info("telegram update %d %s: %s", update_id, kind, outcome)
    return Delivery.ACK

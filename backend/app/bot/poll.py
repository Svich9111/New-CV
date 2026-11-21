"""Опрос getUpdates — ТОЛЬКО режим разработки: проверка бота на настоящем Telegram без
публичного https-сервера. В продуктиве обновления приходят на вебхук
(`POST /api/v1/telegram/webhook`, регистрация — `python -m app.bot.setup --webhook-url ...`).

    cd backend && uv run python -m app.bot.poll [--drop-pending]

Порядок: deleteWebhook (необработанные обновления сохраняются; `--drop-pending` — сбросить),
затем долгий опрос getUpdates (timeout 25 с, allowed_updates — как в setWebhook). Каждое
обновление проходит тот же путь, что и на вебхуке (`app.bot.processing.process_update`:
идемпотентность по update_id, срок обработки, временные сбои). Offset сдвигается только после
обработки; при временном сбое обновление запрашивается снова после паузы. Сетевые ошибки
getUpdates — повтор с нарастающей паузой. Ctrl+C / SIGTERM — текущее обновление
дорабатывается, обработанные подтверждаются Telegram, процесс завершается (повторный сигнал —
немедленная отмена).

Нужны только FEELINGS_TELEGRAM_BOT_TOKEN и FEELINGS_WEBAPP_URL (https); секрет вебхука и БД
не нужны. В журнал не попадают токен, payload и тексты (фильтр `app.core.logging`)."""

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Sequence

import httpx

from app.bot.client import BotApi, BotApiError, describe_error
from app.bot.dedup import UpdateDeduplicator
from app.bot.handlers import handle_update
from app.bot.processing import (
    HANDLE_DEADLINE_S,
    Delivery,
    Handler,
    process_update,
    update_id_of,
)
from app.bot.setup import ALLOWED_UPDATES
from app.config import ConfigError, Settings, check_dev_bot_config, get_settings
from app.core.logging import install_log_redaction

DEV_NOTICE = (
    "режим разработки: опрос getUpdates (вебхук бота удаляется); в продуктиве — вебхук "
    "(python -m app.bot.setup --webhook-url https://<домен>/api/v1/telegram/webhook)"
)
POLL_TIMEOUT_S = 25
# Чтение ответа getUpdates должно ждать дольше, чем Telegram держит долгий опрос.
POLL_HTTP_TIMEOUT = httpx.Timeout(POLL_TIMEOUT_S + 10.0, connect=5.0)
CONFIRM_HTTP_TIMEOUT = httpx.Timeout(5.0)
RETRY_DELAYS_S: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)
# Неверный токен: повторять бесполезно.
FATAL_ERROR_CODES = frozenset({401, 404})
# Опрос удаляет вебхук бота, поэтому разрешён только в явно непродуктивных окружениях (A8 1.4 D-5):
# список разрешённых, а не запрещённых — «prd», «live» и т. п. не пройдут. По умолчанию
# Settings.env = "dev": на продуктивном сервере FEELINGS_ENV обязателен и не dev/test/local.
DEV_ENVS = frozenset({"dev", "test", "local"})

log = logging.getLogger("app.bot")


class PollStopped(Exception):
    """Опрос остановлен ошибкой, которую повтор не исправит (неверный токен)."""

    def __init__(self, err: BotApiError) -> None:
        super().__init__(str(err))
        self.error = err


async def _pause(stop: asyncio.Event, delay: float) -> None:
    """Пауза перед повтором, прерываемая остановкой."""
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(delay):
            await stop.wait()


async def _fetch(
    api: BotApi, payload: dict[str, object], stop: asyncio.Event
) -> tuple[bool, object]:
    """getUpdates, прерываемый остановкой: (False, None) — пришла остановка."""
    fetch = asyncio.ensure_future(api.call("getUpdates", payload, http_timeout=POLL_HTTP_TIMEOUT))
    stopper = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({fetch, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (fetch, stopper):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
    if not fetch.done() or fetch.cancelled():
        return False, None
    return True, fetch.result()


async def poll_updates(
    api: BotApi,
    *,
    webapp_url: str,
    stop: asyncio.Event,
    updates: UpdateDeduplicator | None = None,
    poll_timeout: int = POLL_TIMEOUT_S,
    retry_delays: Sequence[float] = RETRY_DELAYS_S,
    handler: Handler = handle_update,
    deadline_s: float = HANDLE_DEADLINE_S,
) -> int | None:
    """Цикл опроса до `stop`. Возвращает offset — следующий ожидаемый update_id."""
    seen = updates if updates is not None else UpdateDeduplicator()
    offset: int | None = None
    sent_offset: int | None = None  # offset последнего успешного getUpdates — уже подтверждён
    failures = 0

    def next_delay() -> float:
        nonlocal failures
        delay = retry_delays[min(failures, len(retry_delays) - 1)]
        failures += 1
        return delay

    while not stop.is_set():
        payload: dict[str, object] = {"timeout": poll_timeout, "allowed_updates": ALLOWED_UPDATES}
        if offset is not None:
            payload["offset"] = offset
        try:
            got, batch = await _fetch(api, payload, stop)
        except BotApiError as err:
            if err.error_code in FATAL_ERROR_CODES:
                raise PollStopped(err) from None
            delay = next_delay()
            if err.error_code == 409:
                log.warning(
                    "getUpdates conflict (another poller or a webhook is active); retry in %.0f s",
                    delay,
                )
            else:
                log.warning("getUpdates failed: %s; retry in %.0f s", err, delay)
            await _pause(stop, delay)
            continue
        if not got:
            break
        sent_offset = offset
        if not isinstance(batch, list):
            # Настоящий Telegram так не отвечает; без паузы повтор шёл бы вплотную (A8 1.4 D-4).
            delay = next_delay()
            log.warning("getUpdates: unexpected result; retry in %.0f s", delay)
            await _pause(stop, delay)
            continue
        retry = False
        for update in batch:
            if stop.is_set():
                break  # необработанные придут при следующем запуске
            if (
                await process_update(
                    api, seen, update, webapp_url, handler=handler, deadline_s=deadline_s
                )
                is Delivery.RETRY
            ):
                retry = True
                break  # offset не сдвигаем: это обновление придёт снова
            uid = update_id_of(update)
            if uid is not None and (offset is None or uid >= offset):
                offset = uid + 1
        if retry:
            delay = next_delay()
            log.warning("telegram update will be fetched again in %.0f s", delay)
            await _pause(stop, delay)
        else:
            failures = 0

    if offset is not None and offset != sent_offset:
        # Подтвердить обработанные, чтобы следующий запуск не получил их снова.
        try:
            await api.call(
                "getUpdates",
                {"offset": offset, "timeout": 0, "limit": 1, "allowed_updates": ALLOWED_UPDATES},
                http_timeout=CONFIRM_HTTP_TIMEOUT,
            )
            log.info("processed updates confirmed")
        except BotApiError as err:
            log.warning("confirming processed updates failed: %s", err)
    return offset


def check_poll_config(settings: Settings) -> None:
    """Только разработка: токен и https-адрес Mini App; секрет вебхука и БД не нужны."""
    check_dev_bot_config(settings)
    if settings.env.strip().lower() not in DEV_ENVS:
        raise ConfigError(
            "app.bot.poll is for development only (FEELINGS_ENV must be dev, test or local); "
            "production uses the webhook"
        )


async def run(
    settings: Settings,
    *,
    drop_pending: bool = False,
    stop: asyncio.Event | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    retry_delays: Sequence[float] = RETRY_DELAYS_S,
) -> int | None:
    check_poll_config(settings)
    log.warning(DEV_NOTICE)
    stop = stop if stop is not None else asyncio.Event()
    api = BotApi(settings.telegram_bot_token, transport=transport)
    try:
        await api.call("deleteWebhook", {"drop_pending_updates": drop_pending})
        log.info("webhook deleted; pending updates %s", "dropped" if drop_pending else "kept")
        log.info("polling getUpdates; Ctrl+C to stop")
        offset = await poll_updates(
            api, webapp_url=settings.webapp_url, stop=stop, retry_delays=retry_delays
        )
        log.info("polling stopped")
        return offset
    finally:
        await api.aclose()


_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def _install_signal_handlers(stop: asyncio.Event) -> list[signal.Signals]:
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()

    def on_signal() -> None:
        if stop.is_set() and main_task is not None:
            main_task.cancel()  # повторный сигнал — не ждать окончания обработки
        stop.set()

    installed = []
    for sig in _SIGNALS:
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, on_signal)
            installed.append(sig)
    return installed


async def _amain(drop_pending: bool) -> None:
    stop = asyncio.Event()
    installed = _install_signal_handlers(stop)
    try:
        await run(get_settings(), drop_pending=drop_pending, stop=stop)
    finally:
        loop = asyncio.get_running_loop()
        for sig in installed:
            loop.remove_signal_handler(sig)


def main(argv: list[str] | None = None) -> None:
    install_log_redaction()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Опрос getUpdates — только режим разработки; в продуктиве — вебхук"
    )
    parser.add_argument(
        "--drop-pending",
        action="store_true",
        help="при удалении вебхука сбросить накопившиеся обновления",
    )
    args = parser.parse_args(argv)
    print(f"«Чувства AI», бот: {DEV_NOTICE}", file=sys.stderr)
    try:
        asyncio.run(_amain(args.drop_pending))
    except KeyboardInterrupt:
        pass  # платформа без add_signal_handler
    except asyncio.CancelledError:
        pass  # повторный сигнал
    except ConfigError as err:
        sys.exit(f"Настройки: {err}")
    except PollStopped as err:
        sys.exit(describe_error(err.error))
    except BotApiError as err:
        sys.exit(describe_error(err))


if __name__ == "__main__":
    main()

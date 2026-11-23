"""Настройка бота в Telegram: кнопка меню Mini App, список команд и (по желанию) вебхук.

Запускает оператор после подтверждения (регистрация вебхука на стенде — задача 0.8):

    cd backend && uv run python -m app.bot.setup --webhook-url https://<домен>/api/v1/telegram/webhook

Без сервера (режим разработки с `python -m app.bot.poll`) — только команды и кнопка меню,
вебхук не трогается; секрет вебхука не нужен:

    cd backend && uv run python -m app.bot.setup --no-webhook

Токен бота, секрет вебхука и адрес Mini App берутся из окружения (FEELINGS_*); в вывод и журнал
не попадают."""

import argparse
import asyncio
import logging
import sys
from urllib.parse import urlsplit

from app.bot.client import BotApi, BotApiError, describe_error
from app.config import ConfigError, check_bot_config, check_dev_bot_config, get_settings
from app.core.logging import install_log_redaction

log = logging.getLogger("app.bot")

MENU_BUTTON_TEXT = "Открыть"
COMMANDS = [
    {"command": "start", "description": "Открыть «Чувства AI»"},
    {"command": "help", "description": "Как пользоваться"},
]
# Боту нужны только сообщения в личном чате (ответ на /start); остальное Telegram не шлёт.
ALLOWED_UPDATES = ["message"]


async def configure_bot(
    api: BotApi, *, webapp_url: str, webhook_url: str | None, webhook_secret: str | None
) -> None:
    if webhook_url is not None:
        if urlsplit(webhook_url).scheme != "https":
            raise ValueError("webhook url must be https")
        if not webhook_secret:
            raise ValueError("webhook secret is required")
    await api.call(
        "setChatMenuButton",
        {
            "menu_button": {
                "type": "web_app",
                "text": MENU_BUTTON_TEXT,
                "web_app": {"url": webapp_url},
            }
        },
    )
    await api.call("setMyCommands", {"commands": COMMANDS})
    log.info("bot menu button and commands set")
    if webhook_url is not None:
        await api.call(
            "setWebhook",
            {
                "url": webhook_url,
                "secret_token": webhook_secret,
                "allowed_updates": ALLOWED_UPDATES,
            },
        )
        log.info("bot webhook set")


async def _main(webhook_url: str | None, *, no_webhook: bool = False) -> str:
    settings = get_settings()
    if no_webhook:
        check_dev_bot_config(settings)  # секрет вебхука не нужен: setWebhook не вызывается
    else:
        check_bot_config(settings)
    api = BotApi(settings.telegram_bot_token)
    try:
        await configure_bot(
            api,
            webapp_url=settings.webapp_url,
            webhook_url=None if no_webhook else webhook_url,
            webhook_secret=None
            if no_webhook
            else settings.telegram_webhook_secret.get_secret_value(),
        )
    finally:
        await api.aclose()
    if webhook_url is not None and not no_webhook:
        return "Готово: кнопка меню, команды и вебхук заданы."
    return "Готово: кнопка меню и команды заданы; вебхук не менялся."


def main(argv: list[str] | None = None) -> None:
    install_log_redaction()
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Настройка Telegram-бота «Чувства AI»")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--webhook-url", default=None, help="https-адрес вебхука сервера")
    group.add_argument(
        "--no-webhook",
        action="store_true",
        help="только команды и кнопка меню web_app, без вебхука (бот без сервера)",
    )
    args = parser.parse_args(argv)
    try:
        print(asyncio.run(_main(args.webhook_url, no_webhook=args.no_webhook)))
    except ConfigError as err:
        sys.exit(f"Настройки: {err}")
    except BotApiError as err:
        sys.exit(describe_error(err))


__all__ = ["configure_bot", "describe_error", "main"]

if __name__ == "__main__":
    main()

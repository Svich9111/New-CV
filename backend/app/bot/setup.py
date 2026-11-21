"""Настройка бота в Telegram: кнопка меню Mini App, список команд и (по желанию) вебхук.

Запускает оператор после подтверждения (регистрация вебхука на стенде — задача 0.8):

    cd backend && uv run python -m app.bot.setup --webhook-url https://<домен>/api/v1/telegram/webhook

Токен бота, секрет вебхука и адрес Mini App берутся из окружения (FEELINGS_*); в вывод и журнал
не попадают."""

import argparse
import asyncio
import logging
from urllib.parse import urlsplit

from app.bot.client import BotApi
from app.config import check_bot_config, get_settings
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


async def _main(webhook_url: str | None) -> None:
    settings = get_settings()
    check_bot_config(settings)
    api = BotApi(settings.telegram_bot_token)
    try:
        await configure_bot(
            api,
            webapp_url=settings.webapp_url,
            webhook_url=webhook_url,
            webhook_secret=settings.telegram_webhook_secret.get_secret_value(),
        )
    finally:
        await api.aclose()


if __name__ == "__main__":
    install_log_redaction()
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Настройка Telegram-бота «Чувства AI»")
    parser.add_argument("--webhook-url", default=None, help="https-адрес вебхука сервера")
    asyncio.run(_main(parser.parse_args().webhook_url))

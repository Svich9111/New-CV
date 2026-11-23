"""Проверка токена бота: getMe и getWebhookInfo. Выводит имя бота и признак, что токен
рабочий; токен, id бота и адрес вебхука в вывод не попадают.

    cd backend && uv run python -m app.bot.check

Нужен только FEELINGS_TELEGRAM_BOT_TOKEN; БД, секрет вебхука и адрес Mini App не нужны."""

import asyncio
import logging
import sys
from typing import Any

import httpx

from app.bot.client import BotApi, BotApiError, describe_error
from app.config import ConfigError, Settings, check_bot_token_config, get_settings
from app.core.logging import install_log_redaction

# Неверный или отозванный токен.
BAD_TOKEN_CODES = frozenset({401, 404})


class TokenRejected(Exception):
    def __init__(self, err: BotApiError) -> None:
        super().__init__(str(err))
        self.error = err


def _field(data: Any, name: str) -> Any:
    return data.get(name) if isinstance(data, dict) else None


async def check_bot(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> list[str]:
    """Строки отчёта для оператора. Неверный токен — `TokenRejected`, прочие сбои —
    `BotApiError`."""
    check_bot_token_config(settings)
    api = BotApi(settings.telegram_bot_token, transport=transport)
    try:
        try:
            me = await api.call("getMe", {})
        except BotApiError as err:
            if err.error_code in BAD_TOKEN_CODES:
                raise TokenRejected(err) from None
            raise
        hook = await api.call("getWebhookInfo", {})
    finally:
        await api.aclose()
    username = _field(me, "username")
    if not isinstance(username, str) or _field(me, "is_bot") is not True:
        raise BotApiError("getMe", "unexpected response")
    lines = [f"Токен рабочий: бот @{username}", f"Ссылка: https://t.me/{username}"]
    expected = settings.bot_username
    if expected and expected.lower() != username.lower():
        lines.append(
            f"Внимание: FEELINGS_BOT_USERNAME={expected} не совпадает с @{username} — "
            "ссылки-приглашения будут вести не на этого бота"
        )
    elif not expected:
        lines.append(f"Подсказка: задайте FEELINGS_BOT_USERNAME={username}")
    pending = _field(hook, "pending_update_count")
    pending_text = f", ожидают доставки: {pending}" if isinstance(pending, int) else ""
    if _field(hook, "url"):
        lines.append(f"Вебхук: задан{pending_text} (python -m app.bot.poll удалит его)")
    else:
        lines.append(f"Вебхук: не задан{pending_text} (подходит для python -m app.bot.poll)")
    return lines


def main(argv: list[str] | None = None) -> None:
    del argv  # аргументов нет
    install_log_redaction()
    logging.basicConfig(level=logging.WARNING)
    try:
        lines = asyncio.run(check_bot(get_settings()))
    except ConfigError as err:
        sys.exit(f"Настройки: {err}")
    except TokenRejected as err:
        sys.exit(f"Токен не работает: {describe_error(err.error)}")
    except BotApiError as err:
        sys.exit(f"Не удалось проверить токен: {describe_error(err)}")
    print("\n".join(lines))


if __name__ == "__main__":
    main()

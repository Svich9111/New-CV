"""Фильтр журналов: токен приглашения из пути запроса и токен бота из URL Bot API
(`https://api.telegram.org/bot<токен>/<метод>`) не попадают в журналы."""

import logging
import re

_INVITE_PATH = re.compile(r"(/invites/)[^/?#\s\"]+")
# Токен бота: «<число>:<35 символов base64url>»; в URL Bot API идёт после «/bot».
_BOT_TOKEN = re.compile(r"\d{3,}:[A-Za-z0-9_-]{8,}")


def redact(value: str) -> str:
    return _BOT_TOKEN.sub("***", _INVITE_PATH.sub(r"\1***", value))


def _redact_arg(value: object) -> object:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, int | float | bool) or value is None:
        return value
    # Например, httpx.URL в журнале httpx «HTTP Request: POST <url>».
    text = str(value)
    cleaned = redact(text)
    return value if cleaned == text else cleaned


class RedactInviteTokens(logging.Filter):
    """Вырезает токен из `/invites/<токен>` и токен бота в сообщении и аргументах записи."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_arg(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _redact_arg(v) for k, v in record.args.items()}
        return True


# httpx пишет URL каждого запроса на INFO — в URL Bot API есть токен бота.
_LOGGERS = ("uvicorn.access", "uvicorn.error", "httpx", "app.bot")


def install_log_redaction() -> None:
    """Подключает фильтр к журналам uvicorn, httpx и бота один раз."""
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactInviteTokens) for f in logger.filters):
            logger.addFilter(RedactInviteTokens())

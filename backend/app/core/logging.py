"""Фильтр журналов: токен приглашения из пути запроса не попадает в журнал доступа."""

import logging
import re

_INVITE_PATH = re.compile(r"(/invites/)[^/?#\s\"]+")


def redact(value: str) -> str:
    return _INVITE_PATH.sub(r"\1***", value)


class RedactInviteTokens(logging.Filter):
    """Вырезает токен из `/invites/<токен>` в сообщении и аргументах записи."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()
            }
        return True


def install_log_redaction() -> None:
    """Подключает фильтр к журналам uvicorn (доступ и ошибки) один раз."""
    for name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactInviteTokens) for f in logger.filters):
            logger.addFilter(RedactInviteTokens())

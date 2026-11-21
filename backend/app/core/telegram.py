"""Проверка initData Telegram Mini App (ФТ-01, ТЗ §4.1 п.1).

Алгоритм — документация Telegram «Validating data received via the Mini App»:
secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token);
hash = hex(HMAC_SHA256(key=secret_key, msg=data_check_string)), где data_check_string —
пары key=value всех полей, кроме hash, отсортированные по ключу и соединённые "\\n".
initData и его поля в журналы не пишутся.
"""

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode

# Допуск на расхождение часов клиента и сервера при проверке auth_date «из будущего».
CLOCK_SKEW_S = 60
_HASH_RE = re.compile(r"[0-9a-fA-F]{64}")


class InitDataError(Exception):
    """initData не прошло проверку. Причина — только для тестов, клиенту не раскрывается."""


@dataclass(frozen=True)
class TelegramUser:
    id: int
    auth_date: int


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def sign_init_data(fields: dict[str, str], bot_token: str) -> str:
    """Подписать поля как Telegram — для тестов и локальной разработки."""
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    digest = hmac.new(_secret_key(bot_token), dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def validate_init_data(
    init_data: str, bot_token: str, max_age_s: int, now: float | None = None
) -> TelegramUser:
    if not bot_token:
        raise InitDataError("bot token is not configured")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise InitDataError("malformed") from exc
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise InitDataError("duplicate keys")
    fields = dict(pairs)
    received = fields.pop("hash", None)
    if not received:
        raise InitDataError("no hash")
    if not _HASH_RE.fullmatch(received):
        raise InitDataError("bad hash format")
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    expected = hmac.new(_secret_key(bot_token), dcs.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received.lower()):
        raise InitDataError("bad signature")

    try:
        auth_date = int(fields["auth_date"])
        user = json.loads(fields["user"])
        user_id = int(user["id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise InitDataError("missing fields") from exc
    if isinstance(user.get("id"), bool) or user_id <= 0:
        raise InitDataError("bad user id")

    current = time.time() if now is None else now
    if auth_date > current + CLOCK_SKEW_S:
        raise InitDataError("auth_date in future")
    if current - auth_date > max_age_s:
        raise InitDataError("expired")
    return TelegramUser(id=user_id, auth_date=auth_date)

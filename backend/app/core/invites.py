"""Токен приглашения (ФТ-03): 32 случайных байта, base64url без «=»; в БД — только SHA-256."""

import base64
import hashlib
import re
import secrets

TOKEN_BYTES = 32
# Формат пути по контракту (InviteToken: 32–64 символа); алфавит — base64url.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,64}")


def new_token() -> str:
    raw = secrets.token_bytes(TOKEN_BYTES)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def is_well_formed(token: str) -> bool:
    return _TOKEN_RE.fullmatch(token) is not None


def hash_token(token: str) -> bytes:
    """Хеш строки токена — единственное, что уходит в БД."""
    return hashlib.sha256(token.encode("ascii")).digest()


def invite_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?startapp={token}"

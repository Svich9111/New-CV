"""Сессия Mini App: короткоживущий JWT (HS256) после проверки initData."""

import time
import uuid

import jwt

ALGORITHM = "HS256"
AUDIENCE = "feelings-miniapp"


class SessionError(Exception):
    pass


def issue_token(user_id: uuid.UUID, secret: str, ttl_s: int, now: float | None = None) -> str:
    if len(secret) < 32:
        raise SessionError("session secret must be at least 32 characters")
    iat = int(time.time() if now is None else now)
    payload = {"sub": str(user_id), "iat": iat, "exp": iat + ttl_s, "aud": AUDIENCE}
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def read_token(token: str, secret: str) -> uuid.UUID:
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            options={"require": ["sub", "exp", "iat", "aud"]},
        )
        return uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, ValueError) as exc:
        raise SessionError("invalid token") from exc

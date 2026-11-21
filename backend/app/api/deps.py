"""Зависимости FastAPI: БД и текущий пользователь."""

import uuid
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.core.session import SessionError, read_token
from app.errors import AppError

_bearer = HTTPBearer(auto_error=False)


def get_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    sm: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    return sm


def unauthorized() -> AppError:
    return AppError(401, "unauthorized", "Войдите через Telegram заново")


async def current_user_id(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> uuid.UUID:
    if creds is None or creds.scheme.lower() != "bearer":
        raise unauthorized()
    try:
        return read_token(creds.credentials, settings.session_secret.get_secret_value())
    except SessionError as exc:
        raise unauthorized() from exc


SessionMaker = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]
CurrentUser = Annotated[uuid.UUID, Depends(current_user_id)]
AppSettings = Annotated[Settings, Depends(get_settings)]

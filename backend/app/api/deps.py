"""Зависимости FastAPI: БД и текущий пользователь."""

import uuid
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.core.crypto import Keyring
from app.core.session import SessionError, read_token
from app.errors import AppError
from app.repositories import users

_bearer = HTTPBearer(auto_error=False)


def get_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    sm: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    return sm


def get_keyring(request: Request) -> Keyring:
    """Набор ключей шифрования текстов (создаётся при старте, `app.main.lifespan`)."""
    keys: Keyring | None = request.app.state.keyring
    if keys is None:
        raise RuntimeError("encryption keyring is not loaded")
    return keys


def unauthorized() -> AppError:
    return AppError(401, "unauthorized", "Войдите через Telegram заново")


async def current_user_id(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> uuid.UUID:
    if creds is None or creds.scheme.lower() != "bearer":
        raise unauthorized()
    try:
        user_id = read_token(creds.credentials, settings.session_secret.get_secret_value())
    except SessionError as exc:
        raise unauthorized() from exc
    # Удалённый пользователь теряет доступ сразу, не дожидаясь истечения токена (ФТ-21).
    if not await users.is_active(get_sessionmaker(request), user_id):
        raise unauthorized()
    return user_id


async def onboarded_user_id(
    request: Request, user_id: Annotated[uuid.UUID, Depends(current_user_id)]
) -> uuid.UUID:
    """Для эндпоинтов, требующих завершённого онбординга (ФТ-02): без трёх согласий и 18+ —
    403 onboarding_required."""
    if not await users.is_onboarded(get_sessionmaker(request), user_id):
        raise AppError(403, "onboarding_required", "Сначала завершите знакомство с сервисом")
    return user_id


SessionMaker = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]
CurrentUser = Annotated[uuid.UUID, Depends(current_user_id)]
AppSettings = Annotated[Settings, Depends(get_settings)]
OnboardedUser = Annotated[uuid.UUID, Depends(onboarded_user_id)]
Keys = Annotated[Keyring, Depends(get_keyring)]

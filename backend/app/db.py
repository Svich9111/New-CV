"""Подключение к БД и область пользователя.

Каждый запрос к личным данным выполняется внутри `user_scope`: транзакция переключается на
роль `feelings_app` (подчиняется RLS) и получает `app.user_id`. Политики RLS (миграция 0001)
отдают только строки этого пользователя — изоляция обеспечивается базой, а не промптом
(ТЗ §6, §9.2 п.1).
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

APP_ROLE = "feelings_app"

# Транзакция отменена из-за одновременного изменения тех же строк: взаимоблокировка (40P01)
# или конфликт сериализации (40001). Ничего не изменено, запрос можно повторить (АР-24).
RETRYABLE_SQLSTATES = frozenset({"40P01", "40001"})


def sqlstate(exc: DBAPIError) -> str | None:
    """SQLSTATE ошибки БД: у asyncpg — `sqlstate` исходного исключения драйвера."""
    orig = exc.orig
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return str(code) if code is not None else None


def make_engine(url: str) -> AsyncEngine:
    # hide_parameters: параметры запросов (хеши токенов, а позже и тексты) не попадают в
    # текст исключений и журналы (ТЗ §9.1 п.5).
    return create_async_engine(url, pool_pre_ping=True, hide_parameters=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def user_scope(
    sessionmaker: async_sessionmaker[AsyncSession], user_id: uuid.UUID | None
) -> AsyncIterator[AsyncSession]:
    """Транзакция от имени пользователя. `user_id=None` — до входа (видны только функции
    входа). Настройки действуют до конца транзакции (SET LOCAL)."""
    async with sessionmaker() as session, session.begin():
        await session.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
        await session.execute(
            text("SELECT set_config('app.user_id', :uid, true)"),
            {"uid": str(user_id) if user_id else ""},
        )
        yield session

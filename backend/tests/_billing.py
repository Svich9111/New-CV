"""Общий помощник тестов: служебный контекст бота платежей — роль ``feelings_billing`` (0019).

С 0019 (АР-50 п.7; ревью 4.4 э.1 р.1 MINOR-2, A8-4.4-D2) функции ``app_bot_payment_*``
исполняет только ``feelings_billing``: роль входа API (``feelings_api`` → ``feelings_app``) ими
не пользуется. В продуктиве бот входит своей ролью входа (``LOGIN NOINHERIT``, член
``feelings_billing``) и делает ``SET LOCAL ROLE feelings_billing``. В тестах такой роли входа нет
(роли общие для кластера, постоянные тестовые роли не заводятся — CLAUDE.md), поэтому контекст
бота — суперпользователь тестовой БД с тем же ``SET LOCAL ROLE feelings_billing`` и пустым
``app.user_id``: функции видят ту же роль и тот же контекст, что у бота.

``scope_for(sm, None, sql)`` для вызова ``app_bot_payment_*`` даёт этот контекст, для прочего —
``user_scope`` как раньше: помощники ``_call``/``_rows`` с ``uid=None`` («служебный контекст
бота» в тестах 0017) продолжают вызывать функции бота так, как их вызывает бот."""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import ModuleType
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.db import user_scope
from tests.conftest import BACKEND_DIR, TEST_DATABASE_URL

BILLING_ROLE = "feelings_billing"
BOT_CALL_MARK = "app_bot_payment_"

_engine: AsyncEngine | None = None


def _billing_engine() -> AsyncEngine:
    # NullPool: новое соединение на каждый вызов — без привязки пула к циклу событий теста.
    global _engine
    if _engine is None:
        _engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, hide_parameters=True)
    return _engine


@asynccontextmanager
async def billing_scope() -> AsyncIterator[AsyncSession]:
    """Транзакция бота платежей: ``SET LOCAL ROLE feelings_billing``, ``app.user_id`` пуст."""
    async with AsyncSession(_billing_engine()) as session, session.begin():
        await session.execute(text(f"SET LOCAL ROLE {BILLING_ROLE}"))
        await session.execute(text("SELECT set_config('app.user_id', '', true)"))
        yield session


def scope_for(
    sm: async_sessionmaker[AsyncSession], uid: uuid.UUID | None, sql: str
) -> AbstractAsyncContextManager[AsyncSession]:
    """Контекст вызова: функции бота без пользователя — роль платежей, прочее — ``user_scope``."""
    if uid is None and BOT_CALL_MARK in sql:
        return billing_scope()
    return user_scope(sm, uid)


def alembic_on(sync_conn: Any, *args: str) -> None:
    """Команда Alembic на **этом** соединении, внутри уже открытой транзакции теста (env.py берёт
    ``config.attributes['connection']``) — всё откатывается вместе с ней (роли кластера не
    меняются, CLAUDE.md). Без файла ini: env.py не вызывает ``fileConfig`` и не трогает журналы
    процесса тестов (как ``_r2_alembic`` A8)."""
    import argparse

    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.cmd_opts = argparse.Namespace(x=[f"db_url={TEST_DATABASE_URL}"])
    cfg.attributes["connection"] = sync_conn
    getattr(command, args[0])(cfg, *args[1:])


def migration_module(filename: str) -> ModuleType:
    """Модуль ревизии (или ``migrations/billing_guard.py``) по имени файла."""
    base = BACKEND_DIR / "migrations"
    path = base / filename if (base / filename).exists() else base / "versions" / filename
    spec = importlib.util.spec_from_file_location(f"_test_mig_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"не найден модуль: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

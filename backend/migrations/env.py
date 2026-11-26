"""Alembic env. Адрес БД — из FEELINGS_DATABASE_URL или параметра -x db_url=... (CLAUDE.md: без секретов в репозитории)."""

import asyncio
import importlib.util
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

db_url = (
    context.get_x_argument(as_dictionary=True).get("db_url")
    or get_settings().migration_database_url
)
if not db_url:
    raise RuntimeError("FEELINGS_MIGRATION_DATABASE_URL is empty (or pass -x db_url=...)")
config.set_main_option("sqlalchemy.url", db_url)
target_metadata = Base.metadata


def _billing_guard_sql() -> str:
    """Страж роли платежей (``migrations/billing_guard.py``; АР-50 п.7, ревью 4.4 р.2 MAJOR-1):
    исполняется при **каждом** запуске Alembic до миграций, в той же транзакции."""
    path = Path(__file__).with_name("billing_guard.py")
    spec = importlib.util.spec_from_file_location("_feelings_billing_guard", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"не найден страж роли платежей: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return str(mod.MIGRATION_GUARD_SQL)


BILLING_GUARD_SQL = _billing_guard_sql()


def run_migrations_offline() -> None:
    context.configure(url=db_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.execute(BILLING_GUARD_SQL)
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.execute(BILLING_GUARD_SQL)
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is not None:
        do_run_migrations(connectable)
        return
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

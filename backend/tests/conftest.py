"""Общие фикстуры. БД для тестов — TEST_DATABASE_URL (PostgreSQL 16 + pgvector).

Схема создаётся настоящей миграцией Alembic (а не create_all), чтобы тесты проверяли то,
что будет развёрнуто. Данные — только выдуманные (CLAUDE.md)."""

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db import make_engine, make_sessionmaker

BACKEND_DIR = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/feelings_test"
)


def alembic(*args: str) -> None:
    subprocess.run(  # noqa: S603 — фиксированная команда, аргументы из теста
        [sys.executable, "-m", "alembic", "-x", f"db_url={TEST_DATABASE_URL}", *args],
        cwd=BACKEND_DIR,
        check=True,
        capture_output=True,
    )


# Роль входа приложения (АР-10). В продуктиве пароль задаёт инфраструктура; здесь — тестовый.
API_ROLE_PASSWORD = "feelings_api_test_only"


def app_database_url(url: str) -> str:
    """Тот же сервер и БД, но под ролью входа feelings_api (без суперпользователя)."""
    scheme, rest = url.split("://", 1)
    host = rest.split("@", 1)[1]
    return f"{scheme}://feelings_api:{API_ROLE_PASSWORD}@{host}"


@pytest.fixture(scope="session")
async def migrated_db() -> str:
    alembic("downgrade", "base")
    alembic("upgrade", "head")
    eng = make_engine(TEST_DATABASE_URL)
    async with eng.begin() as conn:
        await conn.execute(text(f"ALTER ROLE feelings_api LOGIN PASSWORD '{API_ROLE_PASSWORD}'"))
    await eng.dispose()
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    """Суперпользователь: только для подготовки данных и проверок схемы в обход RLS."""
    eng = make_engine(migrated_db)
    yield eng
    await eng.dispose()


@pytest.fixture(scope="session")
async def app_engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    """Подключение как у приложения: роль входа feelings_api."""
    eng = make_engine(app_database_url(migrated_db))
    yield eng
    await eng.dispose()


@pytest.fixture(scope="session")
def sessionmaker(app_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(app_engine)


# Выдуманный тестовый набор ключей шифрования (задача 4.2): без него сервис не стартует, поэтому
# тесты запуска (lifespan) получают его из окружения. Тесты отказа старта убирают его сами.
TEST_ENCRYPTION_KEYS = "t1:" + "A" * 43


@pytest.fixture(autouse=True)
def _encryption_keys_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings.env по умолчанию — продуктив (2.1 э.2 MINOR-4): тесты — явно окружение test.
    monkeypatch.setenv("FEELINGS_ENV", "test")
    monkeypatch.setenv("FEELINGS_ENCRYPTION_KEYS", TEST_ENCRYPTION_KEYS)
    monkeypatch.setenv("FEELINGS_ENCRYPTION_KEY_ID", "t1")


# Фикстура world (пара Анна и Игорь, посторонняя Ольга) доступна всем модулям тестов.
from tests.test_rls_isolation import world  # noqa: E402, F401

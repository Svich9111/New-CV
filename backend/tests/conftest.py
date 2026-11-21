"""Общие фикстуры. БД для тестов — TEST_DATABASE_URL (PostgreSQL 16 + pgvector).

Схема создаётся настоящей миграцией Alembic (а не create_all), чтобы тесты проверяли то,
что будет развёрнуто. Данные — только выдуманные (CLAUDE.md)."""

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
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


@pytest.fixture(scope="session")
def migrated_db() -> str:
    alembic("downgrade", "base")
    alembic("upgrade", "head")
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(migrated_db)
    yield eng
    await eng.dispose()


@pytest.fixture(scope="session")
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(engine)

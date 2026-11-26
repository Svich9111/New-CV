"""Общие фикстуры. БД для тестов — TEST_DATABASE_URL (PostgreSQL 16 + pgvector).

Схема создаётся настоящей миграцией Alembic (а не create_all), чтобы тесты проверяли то,
что будет развёрнуто. Данные — только выдуманные (CLAUDE.md)."""

import base64
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.fingerprint import FingerprintKeys
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
# Выдуманный ключ отпечатков памяти (задача 2.4, АР-36): base64url 32 байт.
TEST_FINGERPRINT_KEY = "Zg" + "B" * 41
TEST_FP = FingerprintKeys(base64.urlsafe_b64decode(TEST_FINGERPRINT_KEY + "="))


@pytest.fixture(autouse=True)
def _encryption_keys_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings.env по умолчанию — продуктив (2.1 э.2 MINOR-4): тесты — явно окружение test.
    monkeypatch.setenv("FEELINGS_ENV", "test")
    monkeypatch.setenv("FEELINGS_ENCRYPTION_KEYS", TEST_ENCRYPTION_KEYS)
    monkeypatch.setenv("FEELINGS_ENCRYPTION_KEY_ID", "t1")
    monkeypatch.setenv("FEELINGS_MEMORY_FINGERPRINT_KEY", TEST_FINGERPRINT_KEY)


# ---------------------------------------------------------------- набор выпуска «изоляция» (4.3)
# Маркер ``isolation`` на существующих тестах — по перечню ``tests/_isolation_set.py`` (без правки
# их файлов); новые ``test_isolation_release*.py`` помечены сами. ``--isolation-min=N`` — режим
# шага CI (ТЗ §9.2 п.4): собрано меньше N тестов набора или хоть один пропущен (skip) — сборка
# красная; xfail — только ``strict=True`` маркером (нестрогий маркер и ``pytest.xfail()`` в теле
# или фикстуре — красный).
#
# Сверка перечня (запись собрала ровно своё число тестов, всего — ``ISOLATION_MIN``) — только в
# режиме выпуска, где собирается весь каталог: в обычном режиме выборочный запуск (id теста,
# ``--lf``, ``--sw``) собирает часть файла, и сверка дала бы ложную ошибку (ревью 4.3 р.1
# MAJOR-1). Между выпусками перечень сверяет с полным сбором тест
# ``test_isolation_release.py::test_isolation_set_matches_full_collection`` (подпроцессом).


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--isolation-min",
        type=int,
        default=None,
        help="4.3: не меньше N тестов с маркером isolation и ни одного пропуска",
    )


_ISOLATION_STATS: dict[str, list[str]] = {
    "selected": [],
    "skipped": [],
    "xfailed": [],
    "xfail_call": [],
    "aborted": [],
}


def _isolation_match(nodeid: str, entry: str) -> bool:
    if entry.endswith(("::", "_")):
        return nodeid.startswith(entry)
    return nodeid == entry or nodeid.startswith(entry + "[")


def isolation_set_problems(nodeids: list[str], marked: int) -> list[str]:
    """Расхождения перечня с полным сбором: запись собрала не своё число тестов; всего тестов
    набора не ``ISOLATION_MIN``. Общая для режима выпуска и теста сверки."""
    from tests._isolation_set import ISOLATION_MIN, ISOLATION_SET

    out: list[str] = []
    for entry, want in ISOLATION_SET.items():
        got = sum(1 for n in nodeids if _isolation_match(n, entry))
        if got != want:
            out.append(f"{entry}: собрано {got}, в перечне {want}")
    if marked != ISOLATION_MIN:
        out.append(f"всего тестов набора {marked}, ISOLATION_MIN = {ISOLATION_MIN}")
    return out


def _lax_xfail(item: pytest.Item) -> bool:
    default = bool(item.config.getini("xfail_strict"))
    return any(not m.kwargs.get("strict", default) for m in item.iter_markers("xfail"))


@pytest.hookimpl(tryfirst=True)  # до отбора по -m: маркер должен быть уже проставлен
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    from tests._isolation_set import ISOLATION_SET

    for item in items:
        if any(_isolation_match(item.nodeid, entry) for entry in ISOLATION_SET):
            item.add_marker(pytest.mark.isolation)
    if config.getoption("--isolation-min") is None:
        return
    marked = [i for i in items if i.get_closest_marker("isolation")]
    problems = isolation_set_problems([i.nodeid for i in items], len(marked))
    lax = [i.nodeid for i in marked if _lax_xfail(i)]
    # pytest вызывает pytest_collection_finish и после ошибки здесь (finally): помечаем, чтобы
    # его проверка не заслонила эту причину.
    _ISOLATION_STATS["aborted"] = ["yes"] if problems or lax else []
    if problems:
        raise pytest.UsageError(
            "tests/_isolation_set.py не совпадает со сбором: " + "; ".join(problems)
        )
    lax = [i.nodeid for i in marked if _lax_xfail(i)]
    if lax:
        raise pytest.UsageError(f"набор isolation: xfail без strict=True: {lax}")


def pytest_collection_finish(session: pytest.Session) -> None:
    need = session.config.getoption("--isolation-min")
    if need is None or _ISOLATION_STATS["aborted"]:
        return
    selected = [i.nodeid for i in session.items if i.get_closest_marker("isolation")]
    _ISOLATION_STATS["selected"] = selected
    if len(selected) < need or len(selected) != len(session.items):
        raise pytest.UsageError(
            f"набор isolation: собрано {len(selected)} из {len(session.items)} выбранных, "
            f"нужно не меньше {need} и только тесты набора (-m isolation)"
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    yield
    if item.config.getoption("--isolation-min") is None:
        return
    # pytest.xfail() в теле или фикстуре прячет падение без strict — в режиме выпуска красный.
    if call.excinfo is not None and call.excinfo.errisinstance(pytest.xfail.Exception):
        _ISOLATION_STATS["xfail_call"].append(item.nodeid)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.skipped:
        key = "xfailed" if hasattr(report, "wasxfail") else "skipped"
        _ISOLATION_STATS[key].append(report.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if session.config.getoption("--isolation-min") is None:
        return
    bad = _ISOLATION_STATS["skipped"] or _ISOLATION_STATS["xfail_call"]
    if bad and exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    if config.getoption("--isolation-min") is None:
        return
    s = _ISOLATION_STATS
    terminalreporter.write_line(
        f"isolation: собрано {len(s['selected'])} (минимум {config.getoption('--isolation-min')}), "
        f"пропущено {len(s['skipped'])}, xfail {len(s['xfailed'])}"
    )
    for nodeid in s["skipped"]:
        terminalreporter.write_line(f"isolation: ПРОПУЩЕН (сборка красная): {nodeid}", red=True)
    for nodeid in s["xfail_call"]:
        terminalreporter.write_line(
            f"isolation: pytest.xfail() без strict (сборка красная): {nodeid}", red=True
        )


# Фикстура world (пара Анна и Игорь, посторонняя Ольга) доступна всем модулям тестов.
from tests.test_rls_isolation import world  # noqa: E402, F401

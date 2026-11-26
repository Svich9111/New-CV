"""Надёжность фоновых воркеров (A7): пульс и health check (Б-85), код выхода 78 при ошибке
настройки и повтор при временной недоступности на старте (Б-90).

- ``app.workers.health``: формат файла пульса (только время, состояние, вид причины — ни текстов,
  ни id), проверка свежести и состояния, CLI для health check контейнера (код выхода);
- пульс всех четырёх воркеров: проход цикла → ``ok``; пауза из-за ошибки настройки (модель,
  токен бота) → ``paused`` (unhealthy); захват не прошёл (БД недоступна) → пульса нет; длинная
  работа в memory/kb — пульс, пока работа в пределах аренды;
- ``app.workers.startup``: ``ConfigError``/неверные настройки/модель отклонила ключ/БД отвергла
  роль входа → код 78 и сообщение без значений; ``LLMUnavailable`` при старте → повтор в процессе
  (пульс ``waiting``), SIGTERM во время ожидания — выход 0.

БД — тестовый кластер (``TEST_DATABASE_URL``); данные выдуманные; сеть не используется (каталог
OpenRouter подменён)."""

# ruff: noqa: S603 — свой интерпретатор и свой код запуска процессов воркеров

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ai.llm import LLMAuthError, LLMConfigError, LLMModelNotFound, LLMUnavailable
from app.config import ConfigError, Settings
from app.db import make_engine, make_sessionmaker
from app.repositories import kb_work
from app.repositories import memory_work as mw
from app.repositories.kb_work import IndexClaim
from app.workers import health
from app.workers.health import Heartbeat, check
from app.workers.kb import IndexOutcome, KbSettings, KbWorker
from app.workers.memory import MemoryWorker, WorkerSettings
from app.workers.startup import (
    EX_CONFIG,
    config_db_error,
    describe_config_error,
    is_config_error,
    raise_if_config_db_error,
    startup_retry,
)
from tests.conftest import TEST_DATABASE_URL
from tests.test_couple_functions import SM
from tests.test_notify_worker import FakeTg, delivered, nworker, tg_error
from tests.test_pipeline_db import P, _pair, _park
from tests.test_pipeline_worker import CALM, Model, send, worker

BACKEND = Path(__file__).resolve().parents[1]
SECRET_TEXT = "Секретная фраза Анны про отпуск в Сочи"


def read_beat(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


async def until(cond: Callable[[], bool], limit_s: float = 20.0) -> None:
    end = time.monotonic() + limit_s
    while not cond():
        assert time.monotonic() < end, "условие не наступило"
        await asyncio.sleep(0.02)


def _beat_state(path: Path) -> str | None:
    try:
        return str(read_beat(path)["state"])
    except (OSError, ValueError, KeyError):
        return None


# =========================================================================== файл пульса


def test_beat_file_has_only_time_state_and_reason_code(tmp_path: Path) -> None:
    """Б-85: в файле пульса — только время, состояние и вид причины (короткий код); текст,
    id и прочее, переданное по ошибке, заменяется на ``other``."""
    path = tmp_path / "hb.json"
    hb = Heartbeat(path, clock=lambda: 1000.0)
    hb.beat("paused", "auth")
    assert read_beat(path) == {"ts": 1000.0, "state": "paused", "reason": "auth"}
    mid = uuid.uuid4()
    for leak in (SECRET_TEXT, str(mid), f"http_401 {mid}", "x" * 40):
        hb.beat("ok", leak)
        raw = path.read_text(encoding="utf-8")
        assert json.loads(raw) == {"ts": 1000.0, "state": "ok", "reason": "other"}
        assert SECRET_TEXT not in raw and str(mid) not in raw
    hb.beat("ok")
    assert read_beat(path) == {"ts": 1000.0, "state": "ok", "reason": None}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["hb.json"]  # временных файлов нет


def test_beat_write_failure_does_not_stop_worker(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Сбой записи пульса (нет каталога, нет места) не роняет воркер: одно предупреждение."""
    hb = Heartbeat(tmp_path / "missing" / "hb.json")
    with caplog.at_level(logging.WARNING, logger="app.workers.health"):
        for _ in range(3):
            hb.beat("ok")
    assert [r.getMessage() for r in caplog.records] == [
        "worker heartbeat write failed: FileNotFoundError"
    ]


def test_disabled_heartbeat_writes_nothing(tmp_path: Path) -> None:
    hb = Heartbeat(None)
    hb.beat("paused", "auth")
    assert hb.state == "paused" and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("state", "age_s", "ok", "needle"),
    [
        ("ok", 5, True, "state=ok"),
        ("ok", 500, False, "stale"),
        ("paused", 1, False, "state=paused reason=auth"),
        ("waiting", 1, False, "state=waiting"),
        ("starting", 1, False, "state=starting"),
    ],
)
def test_check_fresh_and_working_only(
    tmp_path: Path, state: str, age_s: float, ok: bool, needle: str
) -> None:
    """Healthy — только свежий пульс в состоянии ``ok``; пауза из-за ошибки настройки, ожидание
    при старте и устаревший пульс (цикл завис, БД недоступна) — unhealthy."""
    path = tmp_path / "hb.json"
    reason = "auth" if state == "paused" else None
    path.write_text(json.dumps({"ts": 10_000.0, "state": state, "reason": reason}))
    got, detail = check(path, max_age_s=120, now=10_000.0 + age_s)
    assert got is ok and needle in detail


@pytest.mark.parametrize("body", [None, "", "{", '{"state": "ok"}', '{"ts": "x", "state": "ok"}'])
def test_check_missing_or_broken_file_is_unhealthy(tmp_path: Path, body: str | None) -> None:
    path = tmp_path / "hb.json"
    if body is not None:
        path.write_text(body)
    ok, detail = check(path)
    assert not ok and detail in {"no heartbeat yet", "heartbeat unreadable"}


def _health_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.workers.health", *args], cwd=BACKEND,
        env={**os.environ, "PYTHONPATH": str(BACKEND), **(env or {})},
        capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip


def test_health_cli_exit_codes(tmp_path: Path) -> None:
    """Health check контейнера: ``python -m app.workers.health`` — код 0 у живого воркера, 1 —
    у устаревшего пульса или на паузе; путь — ``FEELINGS_WORKER_HEARTBEAT_FILE`` или ``--file``."""
    path = tmp_path / "hb.json"
    env = {health.HEARTBEAT_ENV: str(path)}
    r = _health_cli(env=env)
    assert r.returncode == 1 and "no heartbeat yet" in r.stdout
    Heartbeat(path).beat("ok")
    r = _health_cli("--max-age", "60", env=env)
    assert r.returncode == 0 and r.stdout.startswith("healthy: state=ok"), r.stdout + r.stderr
    Heartbeat(path, clock=lambda: time.time() - 90).beat("ok")
    assert _health_cli("--max-age", "60", env=env).returncode == 1
    assert _health_cli("--max-age", "120", env=env).returncode == 0
    Heartbeat(path).beat("paused", "http_401")
    r = _health_cli("--file", str(path))
    assert r.returncode == 1 and "state=paused reason=http_401" in r.stdout


def test_default_heartbeat_path_on_container_tmpfs(monkeypatch: pytest.MonkeyPatch) -> None:
    """На стенде корень контейнера только для чтения, ``/tmp`` — tmpfs: пульс — там."""
    monkeypatch.delenv(health.HEARTBEAT_ENV, raising=False)
    assert health.heartbeat_path() == Path("/tmp/feelings-worker-heartbeat.json")  # noqa: S108


async def test_working_beats_only_within_lease(tmp_path: Path) -> None:
    """Длинная работа (memory, kb): пульс идёт, пока работа в пределах своей аренды; дольше —
    работа считается зависшей, пульс останавливается."""
    path = tmp_path / "hb.json"
    hb = Heartbeat(path)
    t0 = time.time()
    async with hb.working(0.3, interval_s=0.05):
        await asyncio.sleep(0.15)
        ts1 = read_beat(path)["ts"]
        await asyncio.sleep(0.45)
        ts2 = read_beat(path)["ts"]
        await asyncio.sleep(0.2)
        ts3 = read_beat(path)["ts"]
    assert ts1 < ts2 == ts3
    assert ts2 - t0 <= 0.45


# =========================================================================== пульс воркеров


@pytest.fixture
async def pair(sessionmaker: SM, engine: AsyncEngine) -> tuple[P, P, uuid.UUID]:
    await _park(engine)
    return await _pair(sessionmaker, engine)


async def _run_for(loop: Any, stop: asyncio.Event, cond: Callable[[], bool]) -> None:
    task = asyncio.create_task(loop)
    try:
        await until(cond)
    finally:
        stop.set()
        await asyncio.wait_for(task, 20)


async def test_pipeline_pulse_ok_then_paused_on_model_config_error(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID], tmp_path: Path
) -> None:
    """Конвейер: проход цикла — ``ok``; ключ модели отклонён (выключатель АР-45 п.2) — воркер на
    паузе, пульс ``paused`` с видом ошибки: health check — unhealthy, хотя процесс жив."""
    anna, _igor, _ = pair
    path = tmp_path / "hb.json"
    hb = Heartbeat(path)
    w = worker(engine, Model(fail={"safety": [LLMAuthError("rejected")]}))
    w.heartbeat = hb
    stop = asyncio.Event()
    task = asyncio.create_task(w.run_forever(poll_s=0.05, stop=stop))
    try:
        await until(lambda: _beat_state(path) == "ok")
        assert check(path, max_age_s=60)[0]
        await send(sessionmaker, anna, CALM)
        await until(lambda: _beat_state(path) == "paused")
    finally:
        stop.set()
        await asyncio.wait_for(task, 20)
    assert read_beat(path)["reason"] == "auth"
    ok, detail = check(path, max_age_s=60)
    assert not ok and "state=paused reason=auth" in detail


async def test_pipeline_no_pulse_while_database_unavailable(tmp_path: Path) -> None:
    """Захват не прошёл (БД недоступна) — проход не успешный: пульса нет, health check со
    временем — unhealthy (раньше подключение к БД в health check было единственным признаком)."""
    path = tmp_path / "hb.json"
    eng = make_engine("postgresql+asyncpg://nobody:x@127.0.0.1:1/none")
    try:
        w = worker(eng, Model())
        w.heartbeat = Heartbeat(path)
        stop = asyncio.Event()
        task = asyncio.create_task(w.run_forever(poll_s=0.05, stop=stop))
        await asyncio.sleep(0.5)
        stop.set()
        await asyncio.wait_for(task, 10)
    finally:
        await eng.dispose()
    assert not path.exists()


def _with_password(url: str, password: str) -> str:
    u = urlsplit(url)
    netloc = f"{u.username}:{password}@{u.hostname}:{u.port}"
    return urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))


async def test_rejected_worker_login_is_config_error_not_retry() -> None:
    """Б-90: БД отвергла роль входа (неверный пароль, нет базы) — ошибка настройки: цикл не
    крутит «claim failed» бесконечно, а завершается ``ConfigError`` (процесс — код 78)."""
    bad_password = "wrong-A7-" + uuid.uuid4().hex[:8]
    for url, code in (
        (_with_password(TEST_DATABASE_URL, bad_password), "28P01"),
        (TEST_DATABASE_URL.rsplit("/", 1)[0] + "/a7_no_such_db", "3D000"),
    ):
        eng = make_engine(url)
        try:
            w = worker(eng, Model())
            with pytest.raises(ConfigError) as err:
                await asyncio.wait_for(w.run_forever(poll_s=0.05), 20)
        finally:
            await eng.dispose()
        assert f"SQLSTATE {code}" in str(err.value)
        assert bad_password not in str(err.value)


async def test_unreachable_database_is_not_config_error() -> None:
    eng = make_engine("postgresql+asyncpg://nobody:x@127.0.0.1:1/none")
    try:
        async with make_sessionmaker(eng)() as s:
            from sqlalchemy import text

            with pytest.raises(OSError) as err:
                await s.execute(text("SELECT 1"))
    finally:
        await eng.dispose()
    assert config_db_error(err.value) is None
    raise_if_config_db_error(err.value, name="t")  # не ошибка настройки — без исключения


async def test_notify_pulse_paused_on_bad_token_ok_on_flood_limit(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID], tmp_path: Path
) -> None:
    """Уведомления: неверный токен бота (401) — ``paused`` (unhealthy: лечит оператор); пауза
    429 — ``ok`` с причиной ``http_429`` (лимит Telegram проходит сам, воркер исправен)."""
    anna, _igor, _ = pair
    await delivered(sessionmaker, engine, anna)
    path = tmp_path / "hb.json"
    tg = FakeTg(tg_error(401))
    w = nworker(engine, tg)
    w.heartbeat = Heartbeat(path)
    stop = asyncio.Event()
    await _run_for(
        w.run_forever(poll_s=0.05, stop=stop), stop, lambda: _beat_state(path) == "paused"
    )
    assert read_beat(path)["reason"] == "http_401" and not check(path)[0]
    await w.api.aclose()

    await _park(engine)
    await delivered(sessionmaker, engine, anna)
    path2 = tmp_path / "hb2.json"
    tg2 = FakeTg(tg_error(429, retry_after=30))
    w2 = nworker(engine, tg2, sleep=asyncio.sleep)
    w2.heartbeat = Heartbeat(path2)
    stop2 = asyncio.Event()
    await _run_for(
        w2.run_forever(poll_s=0.05, stop=stop2),
        stop2,
        lambda: path2.exists() and read_beat(path2).get("reason") == "http_429",
    )
    await w2.api.aclose()
    assert read_beat(path2)["state"] == "ok" and check(path2)[0]


def _memory_worker(path: Path) -> MemoryWorker:
    from tests.test_memory_worker import FP, KEYS

    return MemoryWorker(
        worker_sm=None,  # type: ignore[arg-type]  # захват подменён
        app_sm=None,  # type: ignore[arg-type]
        keys=KEYS,
        fingerprints=FP,
        models=None,  # type: ignore[arg-type]
        settings=WorkerSettings(lease=timedelta(seconds=0.3)),
        heartbeat=Heartbeat(path),
    )


async def test_memory_pulse_after_pass_none_when_claim_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Память: успешный проход — пульс ``ok``; захват упал (БД) — пульса нет; БД отвергла роль
    входа — ``ConfigError`` из цикла (процесс — код 78)."""
    path = tmp_path / "hb.json"

    async def empty(*a: Any, **k: Any) -> list[Any]:
        return []

    monkeypatch.setattr(mw, "claim_extraction", empty)
    monkeypatch.setattr(mw, "claim_compaction", empty)
    w = _memory_worker(path)
    assert await w.run_once() == []
    assert read_beat(path)["state"] == "ok"
    path.unlink()

    async def down(*a: Any, **k: Any) -> list[Any]:
        raise ConnectionRefusedError

    monkeypatch.setattr(mw, "claim_extraction", down)
    stop = asyncio.Event()
    task = asyncio.create_task(w.run_forever(poll_s=0.05, stop=stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert not path.exists()

    async def rejected(*a: Any, **k: Any) -> list[Any]:
        raise _dbapi_error("28P01")

    monkeypatch.setattr(mw, "claim_extraction", rejected)
    with pytest.raises(ConfigError, match="SQLSTATE 28P01"):
        await asyncio.wait_for(w.run_forever(poll_s=0.05), 5)


def _dbapi_error(code: str) -> Exception:
    from sqlalchemy.exc import DBAPIError

    class Orig(Exception):
        sqlstate = code

    return DBAPIError("SELECT 1", None, Orig("rejected"))


async def test_kb_pulse_during_long_index_within_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """БЗ: индексация дольше интервала пульса — пульс идёт, пока она в пределах аренды."""
    path = tmp_path / "hb.json"
    claim = IndexClaim(uuid.uuid4(), uuid.uuid4(), 1, "txt", 10)
    calls = {"n": 0}

    async def one(*a: Any, **k: Any) -> list[IndexClaim]:
        calls["n"] += 1
        return [claim] if calls["n"] == 1 else []

    seen: list[float] = []

    async def slow_index(self: KbWorker, c: IndexClaim) -> IndexOutcome:
        for _ in range(4):
            await asyncio.sleep(0.1)
            seen.append(read_beat(path)["ts"])
        return IndexOutcome("indexed", None, 1)

    monkeypatch.setattr(kb_work, "claim_index", one)
    monkeypatch.setattr(KbWorker, "index", slow_index)
    w = KbWorker(worker_sm=None, settings=KbSettings(lease=timedelta(seconds=30)))  # type: ignore[arg-type]
    w.heartbeat = Heartbeat(path)
    # Интервал пульса в работе — 15 с по умолчанию; для теста — короче.
    original = Heartbeat.working

    def fast(self: Heartbeat, limit_s: float, *, interval_s: float = 15.0) -> Any:
        return original(self, limit_s, interval_s=0.05)

    monkeypatch.setattr(Heartbeat, "working", fast)
    out = await w.run_once()
    assert [o.status for o in out] == ["indexed"]
    assert len(set(seen)) >= 3  # метка обновлялась во время индексации
    assert read_beat(path)["state"] == "ok"


# =========================================================================== запуск: 78 и повтор


class _Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)
        await asyncio.sleep(0)


async def test_startup_retry_waits_on_unavailable_model(tmp_path: Path) -> None:
    """Б-90: каталог OpenRouter недоступен при старте — повтор в процессе с растущей паузой;
    пульс ``waiting`` (unhealthy, без перезапуска контейнера)."""
    path = tmp_path / "hb.json"
    hb = Heartbeat(path)
    states: list[str | None] = []
    left = [LLMUnavailable("down"), LLMUnavailable("down"), LLMUnavailable("down")]

    async def step() -> str:
        states.append(_beat_state(path))
        if left:
            raise left.pop(0)
        return "models"

    sleeps = _Sleeps()
    got = await startup_retry(
        step, name="t", what="models", heartbeat=hb, stop=asyncio.Event(), sleep=sleeps
    )
    assert got == "models"
    assert sleeps.calls == [1.0, 2.0, 4.0]
    assert states == [None, "waiting", "waiting", "waiting"]
    assert read_beat(path)["reason"] == "models_unavailable"


async def test_startup_retry_waits_on_unreachable_database(tmp_path: Path) -> None:
    hb = Heartbeat(tmp_path / "hb.json")
    left: list[Exception] = [ConnectionRefusedError()]

    async def step() -> int:
        if left:
            raise left.pop(0)
        return 1

    assert (
        await startup_retry(
            step, name="t", what="db", heartbeat=hb, stop=asyncio.Event(), sleep=_Sleeps()
        )
        == 1
    )
    assert (hb.state, hb.reason) == ("waiting", "db_unavailable")


@pytest.mark.parametrize(
    "exc",
    [LLMAuthError("k"), LLMConfigError("k"), LLMModelNotFound("m"), ConfigError("x")],
)
async def test_startup_retry_config_error_not_retried(exc: Exception, tmp_path: Path) -> None:
    sleeps = _Sleeps()

    async def step() -> None:
        raise exc

    with pytest.raises(type(exc)):
        await startup_retry(
            step, name="t", what="models", heartbeat=Heartbeat(tmp_path / "hb.json"),
            stop=asyncio.Event(), sleep=sleeps,
        )  # fmt: skip
    assert sleeps.calls == [] and is_config_error(exc)


async def test_startup_retry_rejected_login_is_config_error(tmp_path: Path) -> None:
    async def step() -> None:
        raise _dbapi_error("28P01")

    with pytest.raises(ConfigError, match="28P01"):
        await startup_retry(
            step, name="t", what="db", heartbeat=Heartbeat(None), stop=asyncio.Event()
        )


async def test_startup_retry_stop_interrupts_wait() -> None:
    """SIGTERM во время ожидания при старте — сразу ``None`` (процесс выходит с кодом 0)."""
    stop = asyncio.Event()

    async def step() -> None:
        asyncio.get_running_loop().call_later(0.1, stop.set)
        raise LLMUnavailable("down")

    t0 = time.monotonic()
    assert (
        await startup_retry(step, name="t", what="m", heartbeat=Heartbeat(None), stop=stop) is None
    )
    assert time.monotonic() - t0 < 0.9  # первая пауза — 1 с: прервана остановкой


def test_describe_config_error_has_names_not_values() -> None:
    """Сообщение об ошибке настройки — имя переменной, без значения (секрет мог попасть не в
    то поле)."""
    secret = "sk-or-SECRET-A7-" + uuid.uuid4().hex
    with pytest.raises(ValidationError) as err:
        Settings(pipeline_lease_s=secret)  # type: ignore[arg-type]
    text = describe_config_error(err.value)
    assert text == "invalid settings: FEELINGS_PIPELINE_LEASE_S"
    assert secret not in text and is_config_error(err.value)
    assert describe_config_error(LLMAuthError(f"key {secret} rejected")) == "model provider: auth"
    assert not is_config_error(LLMUnavailable("down")) and not is_config_error(OSError())


def _proc_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    proxy = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("FEELINGS_", "OPENROUTER")) and k not in proxy
    }
    return (
        env
        | {
            "FEELINGS_ENV": "dev",
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(BACKEND),
            health.HEARTBEAT_ENV: str(tmp_path / "hb.json"),
        }
        | extra
    )


@pytest.mark.parametrize(
    ("module", "extra", "names"),
    [
        ("memory", {}, "FEELINGS_WORKER_DATABASE_URL"),
        ("kb", {}, "FEELINGS_WORKER_DATABASE_URL"),
        ("pipeline", {}, "FEELINGS_WORKER_DATABASE_URL"),
        ("notify", {}, "FEELINGS_TELEGRAM_BOT_TOKEN"),
        ("pipeline", {"FEELINGS_WORKER_DATABASE_URL": "postgresql+asyncpg://w:p@127.0.0.1:1/x"},
         "FEELINGS_PIPELINE_PROMPT_SET"),
        ("notify", {"FEELINGS_NOTIFY_LEASE_S": "SECRET-A7-not-a-number"},
         "FEELINGS_NOTIFY_LEASE_S"),
        ("memory", {"FEELINGS_WORKER_DATABASE_URL": "postgresql+asyncpg://w:p@127.0.0.1:1/x",
                    "FEELINGS_ENCRYPTION_KEYS": "t1:SECRET-A7-short", "FEELINGS_ENCRYPTION_KEY_ID": "t1"},
         "encryption key"),
    ],
)  # fmt: skip
def test_process_config_error_exits_78_without_values(
    tmp_path: Path, module: str, extra: dict[str, str], names: str
) -> None:
    """Б-90: ошибка настройки при старте — код выхода 78 (EX_CONFIG), строка CRITICAL с именем
    переменной; значений (секретов) в выводе нет. Политика restart отличает это от сбоя."""
    r = subprocess.run(
        [sys.executable, "-m", f"app.workers.{module}"], cwd=tmp_path,
        env=_proc_env(tmp_path, **extra), capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    out = r.stdout + r.stderr
    assert r.returncode == EX_CONFIG, out[-1500:]
    assert "configuration error, exit 78 (EX_CONFIG)" in out and names in out, out[-1500:]
    assert "SECRET-A7" not in out and "Traceback" not in out


async def test_process_rejected_db_login_exits_78(tmp_path: Path) -> None:
    """БД отвергла роль входа воркера (неверный пароль) — код 78, пароля в выводе нет."""
    bad = "wrong-A7-" + uuid.uuid4().hex[:8]
    env = _proc_env(
        tmp_path,
        FEELINGS_TELEGRAM_BOT_TOKEN="987654321:AAFakeNotifyTokenA7xxxxxxxxxxxxxxxxxx",
        FEELINGS_WORKER_DATABASE_URL=_with_password(TEST_DATABASE_URL, bad),
        FEELINGS_NOTIFY_POLL_S="0.2",
    )
    r = await asyncio.to_thread(
        subprocess.run, [sys.executable, "-m", "app.workers.notify"], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    out = r.stdout + r.stderr
    assert r.returncode == EX_CONFIG, out[-1500:]
    assert "SQLSTATE 28P01" in out and bad not in out and "AAFakeNotify" not in out


# Процесс конвейера с подменённым каталогом OpenRouter: ``unavailable`` — недоступен (как сбой
# сети при старте), ``stub`` — модели-заглушка (воркер идёт в цикл).
_PIPELINE_LAUNCH = (
    "import asyncio, sys\n"
    "import app.ai.models as m\n"
    "from app.ai.llm import LLMUnavailable\n"
    "async def _models(*a, **k):\n"
    "    if sys.argv[1] == 'unavailable':\n"
    "        raise LLMUnavailable('catalog down')\n"
    "    return None\n"
    "m.openrouter_models_async = _models\n"
    "import app.workers.pipeline as p\n"
    "from app.workers.startup import run_worker\n"
    "run_worker(p._main, name='pipeline worker')\n"
)


def _spawn_pipeline(tmp_path: Path, mode: str, db_url: str) -> subprocess.Popen[str]:
    env = _proc_env(
        tmp_path,
        FEELINGS_PIPELINE_PROMPT_SET="v0",
        FEELINGS_WORKER_DATABASE_URL=db_url,
        FEELINGS_ENCRYPTION_KEYS="t1:" + "A" * 43,
        FEELINGS_ENCRYPTION_KEY_ID="t1",
        FEELINGS_PIPELINE_POLL_S="0.2",
    )
    return subprocess.Popen(
        [sys.executable, "-c", _PIPELINE_LAUNCH, mode], cwd=BACKEND, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip


async def _stop(proc: subprocess.Popen[str]) -> tuple[int, str]:
    try:
        os.kill(proc.pid, signal.SIGTERM)
        code = await asyncio.to_thread(proc.wait, 20)
    finally:
        if proc.poll() is None:
            proc.kill()
        out = proc.communicate(timeout=10)[0]
    return code, out


async def test_process_waits_for_model_at_start_unhealthy_not_exiting(tmp_path: Path) -> None:
    """Б-90: OpenRouter недоступен при старте — процесс не выходит (нет бесконечного
    ``Restarting``), пульс ``waiting`` (health check — unhealthy); SIGTERM — выход 0."""
    path = tmp_path / "hb.json"
    proc = _spawn_pipeline(tmp_path, "unavailable", "postgresql+asyncpg://w:p@127.0.0.1:1/x")
    try:
        await until(lambda: _beat_state(path) == "waiting" or proc.poll() is not None)
        await asyncio.sleep(1.5)  # второй повтор (пауза 1 с) — процесс всё ещё жив
        assert proc.poll() is None
        assert read_beat(path)["reason"] == "models_unavailable"
        r = await asyncio.to_thread(_health_cli, env={health.HEARTBEAT_ENV: str(path)})
        assert r.returncode == 1 and "state=waiting" in r.stdout
    finally:
        code, out = await _stop(proc)
    assert code == 0, out[-1500:]
    assert "models_unavailable at start, retry in" in out and "pipeline worker stopped" in out


async def test_process_pulse_ok_in_loop_and_health_cli_passes(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """Б-85, настоящий процесс: цикл конвейера проходит — пульс ``ok``, health check (та же
    команда, что в compose) — код 0."""
    await _park(engine)
    path = tmp_path / "hb.json"
    proc = _spawn_pipeline(tmp_path, "stub", TEST_DATABASE_URL)
    try:
        await until(lambda: _beat_state(path) == "ok" or proc.poll() is not None)
        assert proc.poll() is None
        r = await asyncio.to_thread(
            _health_cli, "--max-age", "60", env={health.HEARTBEAT_ENV: str(path)}
        )
        assert r.returncode == 0, r.stdout
    finally:
        code, out = await _stop(proc)
    assert code == 0, out[-1500:]

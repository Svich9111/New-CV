"""A8, раунд 1: независимая проверка запуска и остановки воркеров (коммит 66dc14e; Б-85, Б-88,
Б-90, Б-91; DECISIONS.md АР-49; ТЗ §10 «надёжность», §9.1 п.5 «журналы без текстов»).

Что доказывается фактом (не чтением кода):

- пульс устаревает, когда цикл завис (захват не возвращается: БД принимает соединение и молчит;
  задача memory дольше своей аренды) и когда БД недоступна — у всех четырёх воркеров;
- пауза из-за ошибки настройки (ключ модели отклонён, токен бота неверен) — команда health check
  из compose завершается кодом 1;
- файл пульса без текстов и id: подделанная причина с текстом/uuid заменяется на ``other``;
  после настоящего прохода конвейера с секретным текстом в файле нет ни текста, ни id;
- код выхода 78 у каждого из четырёх воркеров при отказе БД во входе (роль не существует —
  28000 на trust, 28P01 на scram), значений секретов (пароль, ключи, токен) в выводе нет;
- повтор при старте (недоступна модель/БД) с растущей паузой и SIGTERM во время ожидания —
  выход 0 у memory, kb, notify (у pipeline — ``test_worker_health.py``);
- серия коротких 429 в пределах окна аренды (настоящие паузы) не расходует попытки;
- остановка notify снимает аренду всех неотправленных (несколько в работе одновременно);
- ``stop_grace_period`` конвейера в обоих compose ≥ аренды (значение из окружения сервиса или
  значение по умолчанию настроек).

Данные выдуманные; сеть наружу не используется (каталог OpenRouter подменён)."""

# ruff: noqa: S603 — свой интерпретатор и свой код запуска процессов воркеров

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ai import memory as ai_memory
from app.ai.llm import LLMAuthError
from app.ai.memory import Outcome
from app.config import Settings
from app.core.crypto import generate_key_entry
from app.db import make_engine, make_sessionmaker
from app.repositories import kb_work
from app.repositories import memory_work as mw
from app.repositories import pipeline_work as pw
from app.workers.health import Heartbeat, check
from app.workers.kb import KbSettings, KbWorker
from app.workers.memory import MemoryWorker, WorkerSettings
from app.workers.startup import EX_CONFIG
from tests.conftest import TEST_DATABASE_URL
from tests.test_couple_functions import SM
from tests.test_notify_worker import FakeTg, _lease, chat_of, delivered, nworker, tg_error
from tests.test_pipeline_db import P, _expire, _msg, _pair, _park
from tests.test_pipeline_worker import CALM, Model, send, worker
from tests.test_worker_health import (
    _beat_state,
    _password_checked,
    _proc_env,
    _role_code,
    read_beat,
    until,
)

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
SECRET_TEXT = "Секретная фраза Ольги про дачу в Тарусе A8"
UNREACHABLE_DB = "postgresql+asyncpg://nobody:x@127.0.0.1:1/none"


@pytest.fixture
async def pair(sessionmaker: SM, engine: AsyncEngine) -> tuple[P, P, uuid.UUID]:
    await _park(engine)
    return await _pair(sessionmaker, engine)


def _health(path: Path, max_age: str = "60") -> subprocess.CompletedProcess[str]:
    """Та же команда, что в health check compose (``python -m app.workers.health``)."""
    return subprocess.run(
        [sys.executable, "-m", "app.workers.health", "--max-age", max_age, "--file", str(path)],
        cwd=BACKEND, env=os.environ | {"PYTHONPATH": str(BACKEND)},
        capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip


def _old_beat(path: Path, age_s: float = 100.0) -> float:
    """Пульс ``ok`` из прошлого (как будто последний успешный проход был ``age_s`` назад)."""
    ts = time.time() - age_s
    Heartbeat(path, clock=lambda: ts).beat("ok")
    return round(ts, 3)


_release = asyncio.Event()  # конец теста: «молчащая БД» закрывает соединения


async def _silent_db() -> tuple[asyncio.Server, str]:
    """«БД», которая принимает TCP-соединение и молчит (зависшая сеть/сервер): захват не
    возвращается — цикл воркера висит на нём."""

    async def hold(_r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            await _release.wait()
        finally:
            w.close()

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"postgresql+asyncpg://w:p@127.0.0.1:{port}/x"


async def _cancel(task: asyncio.Task[Any]) -> None:
    task.cancel()
    try:
        await asyncio.wait_for(task, 10)
    except (asyncio.CancelledError, Exception):  # noqa: S110 — завершение зависшего цикла
        pass


def _workers(eng: AsyncEngine, path: Path) -> dict[str, Any]:
    """Все четыре воркера на одном движке, у каждого свой файл пульса."""
    from tests.test_memory_worker import FP, KEYS

    sm = make_sessionmaker(eng)
    p = worker(eng, Model())
    p.heartbeat = Heartbeat(path.with_name("pipeline.json"))
    n = nworker(eng, FakeTg())
    n.heartbeat = Heartbeat(path.with_name("notify.json"))
    m = MemoryWorker(
        worker_sm=sm, app_sm=sm, keys=KEYS, fingerprints=FP,
        models=None,  # type: ignore[arg-type]  # до модели дело не доходит: захват не проходит
        heartbeat=Heartbeat(path.with_name("memory.json")),
    )  # fmt: skip
    k = KbWorker(worker_sm=sm, heartbeat=Heartbeat(path.with_name("kb.json")))
    return {"pipeline": p, "notify": n, "memory": m, "kb": k}


# =========================================================================== пульс устаревает


@pytest.mark.parametrize("db", ["unreachable", "silent"])
async def test_pulse_goes_stale_for_all_four_workers_when_db_down_or_hung(
    tmp_path: Path, db: str
) -> None:
    """Б-85, АР-49 (1): БД недоступна (отказ соединения) или цикл завис на захвате (соединение
    принято, ответа нет) — метка пульса у всех четырёх воркеров не сдвигается с последнего
    успешного прохода, health check (команда compose) — код 1 «stale»."""
    server = None
    url = UNREACHABLE_DB
    if db == "silent":
        server, url = await _silent_db()
    eng = make_engine(url)
    ws = _workers(eng, tmp_path / "x.json")
    before = {name: _old_beat(w.heartbeat.path) for name, w in ws.items()}
    tasks = [
        asyncio.create_task(w.run_forever(poll_s=0.05, stop=asyncio.Event())) for w in ws.values()
    ]
    try:
        await asyncio.sleep(1.0)
        for name, w in ws.items():
            beat = read_beat(w.heartbeat.path)
            assert (beat["ts"], beat["state"]) == (before[name], "ok"), name
            assert not check(w.heartbeat.path, max_age_s=60)[0], name
            r = await asyncio.to_thread(_health, w.heartbeat.path)
            assert r.returncode == 1 and r.stdout.startswith("unhealthy: stale"), (name, r.stdout)
        if db == "silent":
            assert all(not t.done() for t in tasks), "цикл должен висеть на захвате"
    finally:
        for t in tasks:
            await _cancel(t)
        await ws["notify"].api.aclose()
        await eng.dispose()
        if server is not None:
            _release.set()
            await asyncio.sleep(0.05)
            server.close()
            await server.wait_closed()


async def test_pulse_fresh_then_stale_after_db_goes_down(
    engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Контроль от противного: на живой БД пульс конвейера обновляется (health check — 0); захват
    начинает висеть — метка замирает, через ``--max-age`` health check — 1."""
    await _park(engine)
    path = tmp_path / "hb.json"
    w = worker(engine, Model())
    w.heartbeat = Heartbeat(path)
    real_claim = pw.claim
    hang = asyncio.Event()

    async def claim(*a: Any, **k: Any) -> Any:
        if hang.is_set():
            await asyncio.Event().wait()
        return await real_claim(*a, **k)

    monkeypatch.setattr(pw, "claim", claim)
    task = asyncio.create_task(w.run_forever(poll_s=0.05, stop=asyncio.Event()))
    try:
        await until(lambda: path.exists())
        ts1 = read_beat(path)["ts"]
        await asyncio.sleep(0.3)
        ts2 = read_beat(path)["ts"]
        assert ts2 > ts1, "на живой БД пульс должен идти"
        assert (await asyncio.to_thread(_health, path)).returncode == 0
        hang.set()
        await asyncio.sleep(0.3)
        frozen = read_beat(path)["ts"]
        await asyncio.sleep(1.2)
        assert read_beat(path)["ts"] == frozen
        r = await asyncio.to_thread(_health, path, "1")
        assert r.returncode == 1 and "stale" in r.stdout, r.stdout
    finally:
        await _cancel(task)


async def test_memory_job_hung_beyond_lease_pulse_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Б-85: задача memory зависла (дольше аренды) — последовательный цикл стоит; пульс идёт
    только в пределах аренды, затем замирает (health check — unhealthy)."""
    from tests.test_memory_worker import FP, KEYS

    path = tmp_path / "hb.json"
    calls = 0

    async def one(*a: Any, **k: Any) -> list[Any]:
        nonlocal calls
        calls += 1
        return [object()] if calls == 1 else []

    async def none(*a: Any, **k: Any) -> list[Any]:
        return []

    async def forever(*a: Any, **k: Any) -> Outcome:
        await asyncio.Event().wait()
        raise AssertionError  # pragma: no cover

    monkeypatch.setattr(mw, "claim_extraction", one)
    monkeypatch.setattr(mw, "claim_compaction", none)
    monkeypatch.setattr(ai_memory, "run_extraction", forever)
    w = MemoryWorker(
        worker_sm=None,  # type: ignore[arg-type]
        app_sm=None,  # type: ignore[arg-type]
        keys=KEYS, fingerprints=FP,
        models=None,  # type: ignore[arg-type]
        settings=WorkerSettings(lease=timedelta(seconds=0.4)),
        heartbeat=Heartbeat(path),
    )  # fmt: skip
    task = asyncio.create_task(w.run_forever(poll_s=0.05, stop=asyncio.Event()))
    try:
        await until(lambda: path.exists())
        await asyncio.sleep(0.8)  # аренда 0.4 с истекла
        frozen = read_beat(path)["ts"]
        await asyncio.sleep(0.6)
        assert read_beat(path)["ts"] == frozen and not task.done()
        assert not check(path, max_age_s=0.5)[0]
    finally:
        await _cancel(task)


async def test_kb_hung_index_beyond_lease_pulse_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """То же для kb: индексация дольше аренды материала — пульс замирает."""
    path = tmp_path / "hb.json"
    calls = 0

    async def one(*a: Any, **k: Any) -> list[Any]:
        nonlocal calls
        calls += 1
        return [kb_work.IndexClaim(uuid.uuid4(), uuid.uuid4(), 1, "txt", 10)] if calls == 1 else []

    monkeypatch.setattr(kb_work, "claim_index", one)
    w = KbWorker(
        worker_sm=None,  # type: ignore[arg-type]
        settings=KbSettings(lease=timedelta(seconds=0.4)),
        heartbeat=Heartbeat(path),
    )

    async def forever(_c: Any) -> Any:
        await asyncio.Event().wait()

    monkeypatch.setattr(w, "index", forever)
    task = asyncio.create_task(w.run_forever(poll_s=0.05, stop=asyncio.Event()))
    try:
        await until(lambda: path.exists())
        await asyncio.sleep(0.8)
        frozen = read_beat(path)["ts"]
        await asyncio.sleep(0.6)
        assert read_beat(path)["ts"] == frozen and not task.done()
    finally:
        await _cancel(task)


# =========================================================================== пауза → exit 1


async def test_paused_by_config_error_health_cli_exit_1(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID], tmp_path: Path
) -> None:
    """АР-49 (1): пауза из-за настройки — процесс жив и пульс свежий, но health check compose —
    код 1 (конвейер: ключ модели отклонён; notify: токен бота 401)."""
    anna, _igor, _ = pair
    p_path, n_path = tmp_path / "p.json", tmp_path / "n.json"
    w = worker(engine, Model(fail={"safety": [LLMAuthError("rejected")]}))
    w.heartbeat = Heartbeat(p_path)
    stop = asyncio.Event()
    task = asyncio.create_task(w.run_forever(poll_s=0.05, stop=stop))
    try:
        await send(sessionmaker, anna, CALM)
        await until(lambda: _beat_state(p_path) == "paused")
        await asyncio.sleep(0.2)
        assert time.time() - read_beat(p_path)["ts"] < 5  # пульс свежий
        r = await asyncio.to_thread(_health, p_path)
        assert r.returncode == 1 and r.stdout.startswith("unhealthy: state=paused reason=auth")
    finally:
        stop.set()
        await asyncio.wait_for(task, 20)

    await _park(engine)
    await delivered(sessionmaker, engine, anna)
    n = nworker(engine, FakeTg(tg_error(401)))
    n.heartbeat = Heartbeat(n_path)
    stop2 = asyncio.Event()
    task2 = asyncio.create_task(n.run_forever(poll_s=0.05, stop=stop2))
    try:
        await until(lambda: _beat_state(n_path) == "paused")
        r = await asyncio.to_thread(_health, n_path)
        assert r.returncode == 1 and "state=paused reason=http_401" in r.stdout
    finally:
        stop2.set()
        await asyncio.wait_for(task2, 20)
        await n.api.aclose()


# A8 р.1 MINOR-2 исправлен (A7): ошибка настройки модели у memory — пауза, пульс ``paused``.
async def test_memory_rejected_model_key_during_work_is_unhealthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Б-85: «пауза из-за ошибки настройки (модель: auth/...) — unhealthy». В р.1 memory ошибку
    ``auth`` только журналировала (пульс ``ok``, MINOR-2); с ee8a5ce — пауза, пульс ``paused``."""
    from tests.test_memory_worker import FP, KEYS

    path = tmp_path / "hb.json"

    async def one(*a: Any, **k: Any) -> list[Any]:
        return [object()]

    async def none(*a: Any, **k: Any) -> list[Any]:
        return []

    async def auth(*a: Any, **k: Any) -> Outcome:
        return Outcome("extract", "error", error="auth")

    monkeypatch.setattr(mw, "claim_extraction", one)
    monkeypatch.setattr(mw, "claim_compaction", none)
    monkeypatch.setattr(ai_memory, "run_extraction", auth)
    w = MemoryWorker(
        worker_sm=None,  # type: ignore[arg-type]
        app_sm=None,  # type: ignore[arg-type]
        keys=KEYS, fingerprints=FP,
        models=None,  # type: ignore[arg-type]
        heartbeat=Heartbeat(path),
    )  # fmt: skip
    for _ in range(3):
        await w.run_once()
    assert not check(path, max_age_s=60)[0], read_beat(path)


# =========================================================================== файл пульса


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        (SECRET_TEXT, "other"),
        (str(uuid.uuid4()), "other"),
        ("user 1234", "other"),
        ("a" * 33, "other"),
        ("http_401\nInjected: x", "other"),
        ("/home/user/.env", "other"),
        ("  AUTH  ", "auth"),
        ("http_429", "http_429"),
        ("", "other"),
        (None, None),
    ],
)
def test_forged_reason_replaced_by_other(tmp_path: Path, given: str | None, stored: str) -> None:
    """Б-85: в файл пульса попадает только короткий код ``[a-z0-9_]{1,32}``; текст, uuid,
    путь, перевод строки — ``other``. Ключи файла — только ``ts``, ``state``, ``reason``."""
    path = tmp_path / "hb.json"
    Heartbeat(path).beat("paused", given)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"ts", "state", "reason"}
    assert data["reason"] == stored
    if given:
        assert given.strip() not in path.read_text(encoding="utf-8") or stored == given.strip()


def test_forged_file_with_text_reason_health_output_has_no_text(tmp_path: Path) -> None:
    """Подделанный файл (причина с текстом сообщения) — вывод health check (попадает в
    ``docker inspect``) показывает ``other``, текста нет; состояние не ``ok`` — код 1."""
    path = tmp_path / "hb.json"
    path.write_text(
        json.dumps({"ts": time.time(), "state": "paused", "reason": SECRET_TEXT}), encoding="utf-8"
    )
    r = _health(path)
    assert r.returncode == 1 and "reason=other" in r.stdout and SECRET_TEXT not in r.stdout
    path.write_text(
        json.dumps({"ts": time.time(), "state": "ok", "reason": SECRET_TEXT}), encoding="utf-8"
    )
    r = _health(path)
    assert r.returncode == 0 and "reason=other" in r.stdout and SECRET_TEXT not in r.stdout


async def test_real_pass_heartbeat_has_no_text_or_ids(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID], tmp_path: Path
) -> None:
    """ТЗ §9.1 п.5: после настоящих проходов конвейера (доставка секретного текста) и notify в
    файлах пульса нет ни текста, ни id сообщения, пользователей, пары."""
    anna, igor, couple = pair
    p_path, n_path = tmp_path / "p.json", tmp_path / "n.json"
    mid = await send(sessionmaker, anna, SECRET_TEXT)
    w = worker(engine, Model())
    w.heartbeat = Heartbeat(p_path)
    stop = asyncio.Event()
    task = asyncio.create_task(w.run_forever(poll_s=0.05, stop=stop))
    try:
        end = time.monotonic() + 20
        while (row := await _msg(engine, mid)) is not None and row["status"] == "processing":
            assert time.monotonic() < end, "конвейер не завершил сообщение"
            await asyncio.sleep(0.05)
        assert row is not None and row["status"] == "delivered" and p_path.exists()
    finally:
        stop.set()
        await asyncio.wait_for(task, 20)
    tg = FakeTg()
    n = nworker(engine, tg)
    n.heartbeat = Heartbeat(n_path)
    stop_n = asyncio.Event()
    task_n = asyncio.create_task(n.run_forever(poll_s=0.05, stop=stop_n))
    try:
        await until(lambda: len(tg.sent) == 1 and n_path.exists())
        await asyncio.sleep(0.2)
    finally:
        stop_n.set()
        await asyncio.wait_for(task_n, 20)
    await n.api.aclose()
    assert len(tg.sent) == 1
    chat = await chat_of(engine, igor.user_id)
    for path in (p_path, n_path):
        body = path.read_text(encoding="utf-8")
        for needle in (SECRET_TEXT, "Тарус", str(mid), mid.hex, str(anna.user_id),
                       str(igor.user_id), str(couple), str(chat)):  # fmt: skip
            assert needle not in body, (path.name, needle)
        assert set(json.loads(body)) == {"ts", "state", "reason"}


# =========================================================================== процессы: 78, SIGTERM

# Каталог OpenRouter подменён: ``unavailable`` — недоступен (повтор при старте), ``stub`` —
# объект-заглушка (воркер идёт в цикл; до вызова модели дело не доходит).
_LAUNCH = (
    "import asyncio, importlib, sys, types\n"
    "import app.ai.models as m\n"
    "from app.ai.llm import LLMUnavailable\n"
    "async def _models(*a, **k):\n"
    "    if sys.argv[2] == 'unavailable':\n"
    "        raise LLMUnavailable('catalog down')\n"
    "    return types.SimpleNamespace()\n"
    "m.openrouter_models_async = _models\n"
    "mod = importlib.import_module('app.workers.' + sys.argv[1])\n"
    "from app.workers.startup import run_worker\n"
    "run_worker(mod._main, name=sys.argv[1] + ' worker')\n"
)

SECRET_PW = "SECRET-A8-pw-" + uuid.uuid4().hex[:8]
SECRET_TOKEN = "987654321:AAFakeSECRETA8tokenxxxxxxxxxxxxxxxxxxx"
SECRET_OR_KEY = "sk-or-v1-SECRET-A8-" + uuid.uuid4().hex
ENC_ENTRY = generate_key_entry("a8")
FP_KEY = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
SECRET_VALUES = (
    SECRET_PW,
    SECRET_TOKEN,
    "AAFakeSECRETA8",
    SECRET_OR_KEY,
    ENC_ENTRY.split(":")[1],
    FP_KEY,
)


def _login_url(user: str, password: str) -> str:
    u = urlsplit(TEST_DATABASE_URL)
    return urlunsplit((u.scheme, f"{user}:{password}@{u.hostname}:{u.port}", u.path, "", ""))


def _env(tmp_path: Path, db_url: str) -> dict[str, str]:
    return _proc_env(
        tmp_path,
        FEELINGS_WORKER_DATABASE_URL=db_url,
        FEELINGS_TELEGRAM_BOT_TOKEN=SECRET_TOKEN,
        OPENROUTER_API_KEY=SECRET_OR_KEY,
        FEELINGS_ENCRYPTION_KEYS=ENC_ENTRY,
        FEELINGS_ENCRYPTION_KEY_ID="a8",
        FEELINGS_MEMORY_FINGERPRINT_KEY=FP_KEY,
        FEELINGS_PIPELINE_PROMPT_SET="v0",
        FEELINGS_PIPELINE_POLL_S="0.2",
        FEELINGS_NOTIFY_POLL_S="0.2",
    )


def _spawn(tmp_path: Path, module: str, mode: str, db_url: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _LAUNCH, module, mode], cwd=BACKEND, env=_env(tmp_path, db_url),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip


WORKERS = ("memory", "kb", "pipeline", "notify")


@pytest.mark.parametrize("module", WORKERS)
async def test_each_worker_exits_78_on_rejected_login_without_secret_values(
    tmp_path: Path, module: str
) -> None:
    """Б-90, АР-49 (2): БД отвергла роль входа (роли нет — SQLSTATE 28000 на trust; на scram
    PostgreSQL не раскрывает, есть ли роль, и отвечает 28P01; A0, по отчёту A7 р.1) — каждый из
    четырёх воркеров завершается кодом 78 со строкой CRITICAL; ни пароля, ни ключей, ни токена
    бота в выводе нет, трассировки нет. A8 р.2: код не «любой из двух», а тот, что даёт этот
    кластер (пробный вход, ``_role_code``) — 28P01 на trust был бы ошибкой."""
    url = _login_url("a8_no_such_login_" + uuid.uuid4().hex[:6], SECRET_PW)
    proc = _spawn(tmp_path, module, "stub", url)
    try:
        out = (await asyncio.to_thread(proc.communicate, timeout=60))[0]
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == EX_CONFIG, out[-2000:]
    assert "configuration error, exit 78 (EX_CONFIG)" in out, out
    assert f"SQLSTATE {await _role_code()}" in out, out
    for secret in SECRET_VALUES:
        assert secret not in out, secret
    assert "Traceback" not in out


async def _sigterm(proc: subprocess.Popen[str]) -> tuple[int, str]:
    try:
        os.kill(proc.pid, signal.SIGTERM)
        code = await asyncio.to_thread(proc.wait, 20)
    finally:
        if proc.poll() is None:
            proc.kill()
        out = proc.communicate(timeout=10)[0]
    return code, out


@pytest.mark.parametrize("module", ["memory", "kb"])
async def test_startup_retry_backoff_then_sigterm_exits_0(tmp_path: Path, module: str) -> None:
    """Б-90: модель недоступна при старте — memory и kb не выходят, повторяют с растущей паузой
    (1 с, 2 с …), пульс ``waiting`` (health check — 1); SIGTERM во время ожидания — код 0."""
    path = tmp_path / "hb.json"
    proc = _spawn(tmp_path, module, "unavailable", UNREACHABLE_DB)
    try:
        await until(lambda: _beat_state(path) == "waiting" or proc.poll() is not None)
        await asyncio.sleep(3.3)  # первая пауза 1 с, вторая 2 с — третья попытка
        assert proc.poll() is None
        assert read_beat(path)["reason"] == "models_unavailable"
        r = await asyncio.to_thread(_health, path)
        assert r.returncode == 1 and "state=waiting" in r.stdout
    finally:
        code, out = await _sigterm(proc)
    assert code == 0, out[-1500:]
    pauses = [int(x) for x in re.findall(r"models_unavailable at start, retry in (\d+)s", out)]
    assert pauses[:3] == [1, 2, 4], out[-1500:]
    assert f"{module} worker: SIGTERM received" in out
    for secret in SECRET_VALUES:
        assert secret not in out


async def test_notify_db_unreachable_at_start_sigterm_exits_0(tmp_path: Path) -> None:
    """notify: БД недоступна при старте — процесс не выходит (не код 78: это не ошибка
    настройки), пульс остаётся ``starting`` (health check — 1); SIGTERM — код 0."""
    path = tmp_path / "hb.json"
    proc = _spawn(tmp_path, "notify", "stub", UNREACHABLE_DB)
    try:
        await until(lambda: _beat_state(path) == "starting" or proc.poll() is not None)
        await asyncio.sleep(1.0)
        assert proc.poll() is None and _beat_state(path) == "starting"
        assert (await asyncio.to_thread(_health, path)).returncode == 1
    finally:
        code, out = await _sigterm(proc)
    assert code == 0, out[-1500:]
    assert "notify worker claim failed" in out and "notify worker stopped" in out
    for secret in SECRET_VALUES:
        assert secret not in out


# =========================================================================== notify: 429, остановка


async def test_short_429_series_with_real_pauses_never_spends_attempts(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID]
) -> None:
    """Б-91: короткие 429 с настоящими паузами (каждая короче окна, вместе — дольше окна
    аренды): захват за захватом попытка не расходуется, захватов больше ``max_attempts`` —
    уведомление не помечается без отправки; лимит прошёл — ровно одно, попытка 1."""
    anna, igor, _ = pair
    mid = await delivered(sessionmaker, engine, anna)
    tg = FakeTg(*[tg_error(429, retry_after=1) for _ in range(40)])
    # Окно отправки = аренда / 2 = 1.5 с (аренда короче запаса): в окно — 2 паузы по 1 с.
    w = nworker(engine, tg, sleep=asyncio.sleep, lease=timedelta(seconds=3), max_attempts=2,
                max_sends=10)  # fmt: skip
    rounds = 4
    for _ in range(rounds):
        out = await w.run_once()
        assert [(o.status, o.reason) for o in out] == [("retry", "http_429")]
        held, _left, attempts = await _lease(engine, mid)
        assert not held and attempts == 0
        row = await _msg(engine, mid)
        assert row is not None and row["notify_done_at"] is None
        await _expire(engine, mid)
        w._paused_until = 0.0
    sent_429 = len(tg.requests)
    tg.script.clear()
    out = await w.run_once()
    await w.api.aclose()
    assert [(o.status, o.attempts) for o in out] == [("sent", 1)]
    assert sent_429 >= rounds and len(tg.sent) == 1
    assert tg.sent[0]["chat_id"] == await chat_of(engine, igor.user_id)


async def test_stop_releases_every_unsent_lease(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID]
) -> None:
    """Б-88: остановка notify, когда в работе несколько уведомлений (все ждут повтора после
    5xx) — аренда снята у каждого, попытки не израсходованы; новый процесс отправляет все сразу,
    ровно по одному."""
    anna, igor, _ = pair
    mids = [await delivered(sessionmaker, engine, who) for who in (anna, igor, anna)]
    tg = FakeTg(*[tg_error(502) for _ in mids])
    w = nworker(engine, tg, retry_pause_s=30.0, sleep=asyncio.sleep)
    stop = asyncio.Event()
    task = asyncio.create_task(w.run_forever(poll_s=0.05, stop=stop))
    await until(lambda: len(tg.requests) >= len(mids))
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, 10)
    await w.api.aclose()
    for mid in mids:
        held, left_s, attempts = await _lease(engine, mid)
        assert not held and attempts == 0 and (left_s is None or left_s <= 1.0), mid
    chats = {await chat_of(engine, anna.user_id), await chat_of(engine, igor.user_id)}
    assert {r["chat_id"] for r in tg.requests} <= chats, "захвачены чужие уведомления"
    # Остановка не начала повторов: только первые отправки (все 502), ничего не отправлено.
    assert (len(tg.requests), len(tg.sent)) == (len(mids), 0)
    fresh = nworker(engine, tg)
    out = await fresh.run_once()
    await fresh.api.aclose()
    assert sorted((o.status, o.attempts) for o in out) == [("sent", 1)] * len(mids)
    assert len(tg.sent) == len(mids)


# =========================================================================== compose


def _seconds(value: str) -> float:
    m = re.fullmatch(r"(?:(\d+)m)?(?:(\d+)s)?", str(value))
    assert m and value, value
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


@pytest.mark.parametrize(
    "compose", ["infra/docker-compose.yml", "infra/deploy/docker-compose.prod.yml"]
)
def test_pipeline_stop_grace_not_less_than_lease(compose: str) -> None:
    """Б-88, АР-49 (4): ``stop_grace_period`` конвейера ≥ аренды — значение
    ``FEELINGS_PIPELINE_LEASE_S`` из окружения сервиса, а если оно не задано, — значение по
    умолчанию настроек. У всех четырёх воркеров health check — пульс."""
    services = yaml.safe_load((ROOT / compose).read_text(encoding="utf-8"))["services"]
    svc = services["worker-pipeline"]
    env = svc.get("environment") or {}
    lease = float(
        env.get("FEELINGS_PIPELINE_LEASE_S") or Settings.model_fields["pipeline_lease_s"].default
    )
    assert _seconds(svc["stop_grace_period"]) >= lease, (svc["stop_grace_period"], lease)
    for name in ("worker-memory", "worker-kb", "worker-pipeline", "worker-notify"):
        test = services[name]["healthcheck"]["test"]
        assert test[:4] == ["CMD", "python", "-m", "app.workers.health"], (name, test)
        assert "FEELINGS_WORKER_HEARTBEAT_FILE" not in str(services[name].get("volumes", "")), name


# =========================================================================== раунд 2 (ee8a5ce)
#
# A8 р.2: АР-49 п.5, Б-92 (ТЗ §10 «надёжность», §9.1 п.5). Что доказывается фактом:
# - отказ входа несуществующей роли: SQLSTATE зависит от способа входа кластера (trust — 28000,
#   scram — 28P01), страж выше требует ровно код этого кластера;
# - notify: ``paused`` после 401 не снимают 429, 5xx и сбой сети — только ответ, принявший
#   токен (403, 400); getMe при старте в процессе — 401/404 → код 78 без токена; 5xx/429 —
#   пропуск, процесс работает; в dev getMe не вызывается;
# - memory (настоящая БД и настоящее извлечение): на паузе вызовов модели и захватов нет, после
#   паузы — пробный захват одной задачи, попыток — не больше одной на задачу за паузу (Б-92),
#   сбой сети при пробе ``paused`` не снимает, ответ модели — снимает и обнуляет счётчик;
#   во время пробной задачи пульс ``paused`` (не ``ok``);
# - pipeline: ``paused`` держится после паузы и при недоступной модели, снимается ответом;
# - пульс при занятых слотах — пока *хоть одна* задача моложе аренды (не «все»);
# - метка из будущего — health check (CLI) код 1; в пределах допуска — 0;
# - kb после остановки не захватывает новое.


async def test_missing_role_sqlstate_matches_cluster_auth() -> None:
    """Факт для стража A0: вход несуществующей роли — 28000 на trust и 28P01 на scram (PostgreSQL
    при проверке пароля не раскрывает, есть ли роль). Оба кода — ошибка настройки (код 78)."""
    import asyncpg

    u = urlsplit(TEST_DATABASE_URL)
    scram = await _password_checked()
    with pytest.raises(asyncpg.PostgresError) as err:
        await asyncpg.connect(
            user="a8_no_such_role_" + uuid.uuid4().hex[:6], password=SECRET_PW,
            host=u.hostname, port=u.port, database=u.path.lstrip("/"), timeout=10,
        )  # fmt: skip
    assert err.value.sqlstate == ("28P01" if scram else "28000"), (scram, err.value.sqlstate)
    assert err.value.sqlstate == await _role_code()
    assert SECRET_PW not in str(err.value)


class _ProbeTg(FakeTg):
    """Bot API, который перед каждым ответом записывает, на паузе ли воркер из-за токена."""

    def __init__(self, *script: Any) -> None:
        super().__init__(*script)
        self.worker: Any = None
        self.paused_at_request: list[bool] = []

    async def handler(self, request: Any) -> Any:
        if self.worker is not None:
            self.paused_at_request.append(self.worker.config_paused)
        return await super().handler(request)


@pytest.mark.parametrize("accepted", [403, 400])
async def test_notify_paused_kept_by_429_5xx_network_cleared_only_by_accepted_token(
    sessionmaker: SM,
    engine: AsyncEngine,
    pair: tuple[P, P, uuid.UUID],
    tmp_path: Path,
    accepted: int,
) -> None:
    """АР-49 п.5: после 401 ``paused`` держится, пока Telegram не ответит, приняв токен.
    429, 502 и сбой сети токен не подтверждают; 403/400 — подтверждают (пульс ``ok``)."""
    import httpx

    anna, _igor, _ = pair
    path = tmp_path / "hb.json"
    mid = await delivered(sessionmaker, engine, anna)
    tg = _ProbeTg(
        tg_error(401), tg_error(429, retry_after=1), tg_error(502), httpx.ConnectError,
        tg_error(accepted),
    )  # fmt: skip
    w = nworker(engine, tg, pause=timedelta(seconds=0.05))
    w.heartbeat = Heartbeat(path)
    tg.worker = w
    statuses: list[tuple[str, str | None]] = []
    try:
        for _ in range(20):
            if not tg.script:
                break
            w._paused_until = 0.0  # паузы 401/429 не ждём: проверяется только признак токена
            await asyncio.sleep(0.06)
            await _expire(engine, mid)
            statuses += [(o.status, o.reason) for o in await w.run_once()]
            w._pulse()
            if tg.script:  # ещё не было ответа, принявшего токен
                assert w.config_paused and read_beat(path)["state"] == "paused", statuses
    finally:
        await w.api.aclose()
    assert not tg.script, statuses
    assert statuses[0] == ("paused", "http_401")
    # Перед 2-м…5-м запросом (после 401, 429, 502, сети) — всё ещё пауза из-за токена.
    assert tg.paused_at_request == [False, True, True, True, True], tg.paused_at_request
    assert not w.config_paused
    # ``ok`` (возможно, с причиной ``http_429`` — лимит Telegram проходит сам): healthy.
    assert read_beat(path)["state"] == "ok" and check(path, max_age_s=60)[0]


# --------------------------------------------------------------------------- getMe в процессе

_NOTIFY_TG_LAUNCH = (
    "import sys\n"
    "import app.bot.client as c\n"
    "_init = c.BotApi.__init__\n"
    "def _local(self, *a, **k):\n"
    "    k['base_url'] = sys.argv[1]\n"
    "    _init(self, *a, **k)\n"
    "c.BotApi.__init__ = _local\n"
    "import app.workers.notify as n\n"
    "from app.workers.startup import run_worker\n"
    "run_worker(n._main, name='notify worker')\n"
)


class _LocalTelegram:
    """Локальный «Telegram»: на getMe — заданный код; записывает только имя метода."""

    def __init__(self, code: int) -> None:
        import http.server
        import threading

        self.methods: list[str] = []
        self.paths: list[str] = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.paths.append(self.path)
                outer.methods.append(self.path.rsplit("/", 1)[-1])
                if code == 200:
                    body: dict[str, Any] = {"ok": True, "result": {"id": 1, "is_bot": True}}
                else:
                    body = {"ok": False, "error_code": code, "description": "DESCR-A8 Unauthorized"}
                    if code == 429:
                        body["parameters"] = {"retry_after": 1}
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _spawn_notify_tg(tmp_path: Path, tg: _LocalTelegram, env_name: str) -> subprocess.Popen[str]:
    env = _env(tmp_path, UNREACHABLE_DB) | {"FEELINGS_ENV": env_name}
    env.pop("FEELINGS_NOTIFY_CHECK_TOKEN")  # значение по умолчанию: по окружению
    return subprocess.Popen(
        [sys.executable, "-c", _NOTIFY_TG_LAUNCH, tg.url], cwd=BACKEND, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip


@pytest.mark.parametrize(("env_name", "code"), [("staging", 401), ("production", 404)])
async def test_notify_process_getme_rejected_exits_78_without_token(
    tmp_path: Path, env_name: str, code: int
) -> None:
    """АР-49 п.5: notify на стенде/в продуктиве при старте спрашивает getMe; 401/404 — код 78,
    в выводе только код HTTP — ни токена, ни описания от Telegram, ни трассировки. БД при этом
    недоступна — выход по токену, а не по БД."""
    tg = _LocalTelegram(code)
    proc = _spawn_notify_tg(tmp_path, tg, env_name)
    try:
        out = (await asyncio.to_thread(proc.communicate, timeout=60))[0]
    finally:
        if proc.poll() is None:
            proc.kill()
        tg.close()
    assert proc.returncode == EX_CONFIG, out[-2000:]
    assert tg.methods[:1] == ["getMe"] and tg.methods.count("getMe") == 1, tg.methods
    assert f"Telegram rejected FEELINGS_TELEGRAM_BOT_TOKEN (HTTP {code})" in out, out[-2000:]
    assert "configuration error, exit 78 (EX_CONFIG)" in out
    for secret in SECRET_VALUES:
        assert secret not in out, secret
    assert "DESCR-A8" not in out and "Traceback" not in out


@pytest.mark.parametrize("code", [502, 429])
async def test_notify_process_getme_unavailable_keeps_running(tmp_path: Path, code: int) -> None:
    """5xx/429 на getMe — не ошибка настройки: проверка пропускается с предупреждением, процесс
    работает (не 78); SIGTERM — код 0 и «notify worker stopped»."""
    tg = _LocalTelegram(code)
    proc = _spawn_notify_tg(tmp_path, tg, "staging")
    try:
        await until(lambda: "getMe" in tg.methods or proc.poll() is not None)
        await asyncio.sleep(1.0)
        assert proc.poll() is None
    finally:
        rc, out = await _sigterm(proc)
        tg.close()
    assert rc == 0, out[-2000:]
    assert f"bot token check skipped: http_{code}" in out, out[-2000:]
    assert "notify worker stopped" in out and "Traceback" not in out
    for secret in SECRET_VALUES:
        assert secret not in out, secret


async def test_notify_process_dev_does_not_call_getme(tmp_path: Path) -> None:
    """По умолчанию в dev getMe не вызывается (токен ненастоящий), даже если Telegram ответил бы
    401: процесс работает, SIGTERM — 0."""
    tg = _LocalTelegram(401)
    proc = _spawn_notify_tg(tmp_path, tg, "dev")
    path = tmp_path / "hb.json"
    try:
        await until(lambda: _beat_state(path) is not None or proc.poll() is not None)
        await asyncio.sleep(1.5)
        assert proc.poll() is None
    finally:
        rc, out = await _sigterm(proc)
        tg.close()
    assert rc == 0, out[-2000:]
    assert tg.methods == [], tg.methods


# --------------------------------------------------------------------------- memory: Б-92


async def _sweep_memory_queue(engine: AsyncEngine) -> None:
    """Общая тестовая БД: свободные задачи извлечения других модулей занимаются арендой на 2 мин,
    чтобы в выдачу попадали только агенты этого теста (короткие аренды чужих — 2 с — ждём)."""
    wsm = make_sessionmaker(engine)
    for n in range(2):
        for _ in range(100):
            got = await mw.claim_extraction(
                wsm, gap=timedelta(minutes=1), lease=timedelta(minutes=2), limit=100
            )
            if not got:
                break
        if n == 0:
            await asyncio.sleep(2.2)


async def test_memory_rejected_key_real_db_probe_one_and_one_attempt_per_pause(
    sessionmaker: SM, engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """АР-49 п.5, Б-92 (настоящая БД, настоящее извлечение, модель-заглушка): ключ отклонён —
    остаток пачки без вызовов модели, на паузе захватов нет; после паузы — пробный захват одной
    задачи; за паузу попыток — не больше одной на задачу (всего +1 за пробу); сбой сети на
    пробе ``paused`` не снимает; ответ модели — снимает, счётчик задачи обнуляется, затем —
    полные пачки, всё извлечено."""
    from sqlalchemy import text

    from app.ai.llm import LLMUnavailable
    from app.ai.models import AgentModels
    from tests.test_memory_worker import FP, GAP, KEYS, person, say
    from tests.test_memory_worker import Model as MemModel

    await _sweep_memory_queue(engine)
    who = [await person(engine, sessionmaker, name) for name in ("Вера", "Ира", "Ада")]
    for w_ in who:
        await say(engine, w_, "СЕКРЕТ-A8 реплика", "ответ агента")
    mine = {w_.agent_id for w_ in who}

    class Counting(MemModel):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def complete_json(self, **kw: Any) -> Any:
            self.calls += 1
            return await super().complete_json(**kw)

    model = Counting()
    model.fail = [LLMAuthError("rejected"), LLMAuthError("rejected"), LLMUnavailable("503")]
    real_x = mw.claim_extraction
    limits: list[int] = []
    claimed: list[int] = []

    async def claim_x(*a: Any, **k: Any) -> list[Any]:
        limits.append(int(k["limit"]))
        got = await real_x(*a, **k)
        assert {c.agent_id for c in got} <= mine, "чужой агент в выдаче: очистка не сработала"
        claimed.append(len(got))
        return got

    async def no_compaction(*a: Any, **k: Any) -> list[Any]:
        return []

    monkeypatch.setattr(mw, "claim_extraction", claim_x)
    monkeypatch.setattr(mw, "claim_compaction", no_compaction)
    path = tmp_path / "hb.json"
    lease, pause = timedelta(seconds=0.3), timedelta(seconds=0.5)
    w = MemoryWorker(
        worker_sm=make_sessionmaker(engine), app_sm=sessionmaker, keys=KEYS, fingerprints=FP,
        models=AgentModels.single(model),
        settings=WorkerSettings(session_gap=GAP, lease=lease, pause=pause),
        heartbeat=Heartbeat(path),
    )  # fmt: skip

    async def attempts() -> dict[uuid.UUID, int]:
        async with engine.connect() as c:
            rows = await c.execute(
                text("SELECT id, memory_attempts FROM agents WHERE id = ANY(:a)"),
                {"a": list(mine)},
            )
            return {r.id: int(r.memory_attempts) for r in rows}

    def state() -> tuple[str, str | None]:
        b = read_beat(path)
        return b["state"], b["reason"]

    # 1. Пачка из трёх: первая — auth, остальные две не начаты (вызов модели один).
    out = await w.run_once()
    assert [(o.status, o.error) for o in out] == [("error", "auth")]
    assert (limits, claimed, model.calls) == ([10], [3], 1)
    assert sorted((await attempts()).values()) == [1, 1, 1]
    assert state() == ("paused", "auth") and not check(path, max_age_s=60)[0]
    # 2. На паузе — ни захвата, ни вызова модели.
    for _ in range(3):
        assert await w.run_once() == []
    assert (limits, model.calls) == ([10], 1)
    # 3. Пауза прошла — проба одной задачи, снова auth: +1 попытка всего.
    await asyncio.sleep(pause.total_seconds() + 0.05)
    before = await attempts()
    out = await w.run_once()
    after = await attempts()
    assert [(o.status, o.error) for o in out] == [("error", "auth")]
    assert (limits[-1], claimed[-1], model.calls) == (1, 1, 2)
    assert sum(after.values()) - sum(before.values()) == 1
    assert all(after[a] - before[a] <= 1 for a in mine)
    assert state() == ("paused", "auth")
    # 4. Следующая проба — модель недоступна (сеть): ключ не подтверждён, ``paused`` держится.
    await asyncio.sleep(pause.total_seconds() + 0.05)
    out = await w.run_once()
    assert [(o.status, o.error) for o in out] == [("error", "unavailable")]
    assert (limits[-1], claimed[-1], model.calls) == (1, 1, 3)
    assert state() == ("paused", "auth") and not check(path, max_age_s=60)[0]
    # За три паузы (три окна) у каждой задачи — не больше одной попытки за окно: ≤ 1 + 2.
    assert max((await attempts()).values()) <= 3
    # 5. Модель ответила: проба удачна — ``ok``, счётчик этой задачи обнулён; затем полная пачка.
    await asyncio.sleep(lease.total_seconds() + 0.05)
    out = await w.run_once()
    assert [o.status for o in out][:1] == ["done"] and limits[-1] == 1
    assert state() == ("ok", None)
    for _ in range(5):
        if (await attempts()) == dict.fromkeys(mine, 0):
            break
        await asyncio.sleep(lease.total_seconds() + 0.05)
        await w.run_once()
        assert limits[-1] == 10  # пауза снята — полные пачки
    assert await attempts() == dict.fromkeys(mine, 0)
    assert state() == ("ok", None) and check(path, max_age_s=60)[0]
    assert "СЕКРЕТ-A8" not in path.read_text(encoding="utf-8")


async def test_memory_probe_job_pulse_stays_paused_while_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Во время пробной задачи пульс длинной работы (``working``) пишет последнее состояние —
    ``paused``, а не ``ok``: health check не становится healthy до ответа модели."""
    from tests.test_memory_worker import FP, KEYS

    path = tmp_path / "hb.json"
    seen: list[str] = []

    async def one(*a: Any, **k: Any) -> list[Any]:
        return [object()]

    async def none(*a: Any, **k: Any) -> list[Any]:
        return []

    async def slow(*a: Any, **k: Any) -> Outcome:
        await asyncio.sleep(0.1)
        seen.append(read_beat(path)["state"])
        return Outcome("extract", "error", error="unavailable")

    monkeypatch.setattr(mw, "claim_extraction", one)
    monkeypatch.setattr(mw, "claim_compaction", none)
    monkeypatch.setattr(ai_memory, "run_extraction", slow)
    w = MemoryWorker(
        worker_sm=None,  # type: ignore[arg-type]
        app_sm=None,  # type: ignore[arg-type]
        keys=KEYS, fingerprints=FP,
        models=None,  # type: ignore[arg-type]
        settings=WorkerSettings(lease=timedelta(seconds=5)),
        heartbeat=Heartbeat(path),
    )  # fmt: skip
    w._pause_kind = "auth"  # пауза прошла, ключ ещё не подтверждён
    w.heartbeat.beat("paused", "auth")
    path.unlink()

    real_working = w.heartbeat.working
    ticks: list[str] = []

    def spy(limit_s: float, **k: Any) -> Any:
        ticks.append("working")
        return real_working(limit_s, **k)

    monkeypatch.setattr(w.heartbeat, "working", spy)
    await w.run_once()
    assert ticks == ["working"] and seen == ["paused"], (ticks, seen)
    assert read_beat(path)["state"] == "paused"


# --------------------------------------------------------------------------- pipeline


async def test_pipeline_paused_survives_unavailable_model_until_answer(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID], tmp_path: Path
) -> None:
    """АР-49 п.5: после auth — ``paused``; пауза прошла, модель недоступна (сеть) — ``paused``
    держится и во время отсрочки повтора; модель ответила — ``ok``, сообщение доставлено."""
    from app.ai.llm import LLMUnavailable

    anna, _igor, _ = pair
    path = tmp_path / "hb.json"
    model = Model(fail={"safety": [LLMAuthError("rejected"), LLMUnavailable("503")]})
    w = worker(engine, model, pause=timedelta(seconds=0.3), retry_base=timedelta(seconds=0.4))
    w.heartbeat = Heartbeat(path)
    samples: list[tuple[str | None, int]] = []
    stop = asyncio.Event()

    async def sample() -> None:
        while not stop.is_set():
            samples.append((_beat_state(path), len(model.fail["safety"])))
            await asyncio.sleep(0.01)

    sampler = asyncio.create_task(sample())
    task = asyncio.create_task(w.run_forever(poll_s=0.05, stop=stop))
    try:
        mid = await send(sessionmaker, anna, CALM)
        await until(lambda: _beat_state(path) == "ok" and not model.fail["safety"])
    finally:
        stop.set()
        await asyncio.wait_for(task, 20)
        await sampler
    samples.append((_beat_state(path), len(model.fail["safety"])))  # последний — ``ok``
    # После того как модель ответила «недоступна» — до её ответа пульс только ``paused``.
    after_unavailable = [s for s, left in samples if left == 0]
    first_ok = after_unavailable.index("ok")
    assert after_unavailable[:first_ok] and set(after_unavailable[:first_ok]) == {"paused"}
    from tests.test_pipeline_db import _msg as msg_row

    row = await msg_row(engine, mid)
    assert row is not None and row["status"] == "delivered"


@pytest.mark.parametrize("kind", ["pipeline", "notify"])
async def test_busy_slots_pulse_while_any_one_task_within_lease(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """АР-49 п.5: все слоты заняты, одна задача уже старше аренды, другая моложе — пульс идёт
    («хотя бы одна»); обе старше аренды — пульс замирает."""
    from app.repositories import notify_work as nw
    from app.repositories.notify_work import NotifyClaim
    from app.repositories.pipeline_work import PipelineClaim

    path = tmp_path / "hb.json"
    eng = make_engine(UNREACHABLE_DB)
    lease = timedelta(seconds=0.5)
    t0 = time.monotonic()
    w: Any
    if kind == "pipeline":
        w = worker(eng, Model(), batch=2, lease=lease)
        mk: Any = lambda: PipelineClaim(uuid.uuid4(), uuid.uuid4(), 1)  # noqa: E731
        repo: Any = pw
    else:
        w = nworker(eng, FakeTg(), batch=2, lease=lease, margin=timedelta(seconds=0.1))
        mk = lambda: NotifyClaim(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), 1)  # noqa: E731
        repo = nw
    given = 0

    async def claim(*a: Any, **k: Any) -> list[Any]:
        nonlocal given
        if given == 0 or (given == 1 and time.monotonic() - t0 > 0.3):
            given += 1
            return [mk()]
        return []

    monkeypatch.setattr(repo, "claim", claim)
    release = asyncio.Event()

    async def hang(c: Any) -> Any:
        await release.wait()

    monkeypatch.setattr(w, "_guard", hang)
    w.heartbeat = Heartbeat(path)
    stop = asyncio.Event()
    task = asyncio.create_task(w.run_forever(poll_s=0.05, stop=stop))
    try:
        await until(lambda: given == 2)
        # Первая задача старше аренды (0.5 с) с ~0.5 с, вторая — до ~0.8 с.
        await asyncio.sleep(max(t0 + 0.55 - time.monotonic(), 0.0))
        ts1 = read_beat(path)["ts"]
        await asyncio.sleep(0.12)
        assert read_beat(path)["ts"] > ts1, "одна задача моложе аренды — пульс должен идти"
        await asyncio.sleep(max(t0 + 1.0 - time.monotonic(), 0.0))  # обе старше аренды
        frozen = read_beat(path)["ts"]
        await asyncio.sleep(0.3)
        assert read_beat(path)["ts"] == frozen and not task.done()
    finally:
        stop.set()
        release.set()
        await asyncio.wait_for(task, 10)
        if kind == "notify":
            await w.api.aclose()
        await eng.dispose()


# --------------------------------------------------------------------------- health, kb


@pytest.mark.parametrize(
    ("ahead_s", "state", "code", "text"),
    [(60.0, "ok", 1, "from the future"), (3.0, "ok", 0, "state=ok"),
     (60.0, "paused", 1, "state=paused"), (-3.0, "ok", 0, "state=ok")],
)  # fmt: skip
def test_health_cli_future_timestamp(
    tmp_path: Path, ahead_s: float, state: str, code: int, text: str
) -> None:
    """Метка из будущего дальше 5 с — команда health check compose: код 1; ближе — 0."""
    path = tmp_path / "hb.json"
    ts = time.time() + ahead_s
    path.write_text(json.dumps({"ts": ts, "state": state, "reason": None}), encoding="utf-8")
    r = _health(path)
    assert r.returncode == code and text in r.stdout, r.stdout


async def test_kb_stopped_before_pass_claims_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """kb после SIGTERM: новый проход ничего не захватывает."""
    calls: list[int] = []

    async def claim(*a: Any, **k: Any) -> list[Any]:
        calls.append(int(k["limit"]))
        return [kb_work.IndexClaim(uuid.uuid4(), uuid.uuid4(), 1, "txt", 10)]

    monkeypatch.setattr(kb_work, "claim_index", claim)
    w = KbWorker(worker_sm=None, heartbeat=Heartbeat(None))  # type: ignore[arg-type]
    stop = asyncio.Event()
    stop.set()
    assert await w.run_once(stop) == [] and calls == []
    await asyncio.wait_for(w.run_forever(poll_s=0.05, stop=stop), 5)
    assert calls == []

"""Б-96 (A8): связка запуска воркера уведомлений ``_main → token_check_enabled →
run_with_token_check`` (``app.workers.notify``; ревью воркеров р.2 MINOR-C; DECISIONS.md АР-49 п.5;
ТЗ §10 «надёжность», §9.1 п.5 «журналы без секретов»; ФТ-18).

``_main`` помечен ``pragma: no cover``: потеря флага (``check_token`` не передан, передан
константой, ``token_check_enabled`` сломан) молча выключила бы проверку токена бота (``getMe``)
на стенде и в продуктиве. Что доказывается фактом:

- **связка в процессе** (``_main`` в этом процессе, ``run_with_token_check`` подменён шпионом):
  флаг, который ``_main`` передаёт в цикл, — по ``FEELINGS_ENV`` (staging/production и любое
  не-dev окружение — да; dev/local/test — нет) и ``FEELINGS_NOTIFY_CHECK_TOKEN`` (явное значение
  сильнее окружения); в цикл передан воркер с тем же клиентом Bot API, что закрывается при выходе;
- **процесс целиком** (``python -c`` → ``run_worker(_main)``, Bot API — локальный HTTP-сервер):
  на стенде с принятым токеном первый и единственный запрос к Telegram при старте — ``getMe``,
  в журнале «bot token accepted», процесс работает, SIGTERM — 0; в dev с явным
  ``FEELINGS_NOTIFY_CHECK_TOKEN=true`` — ``getMe`` есть; на стенде с явным ``false`` — нет.

Отказ 401/404 → код 78, 5xx/429 → пропуск и dev по умолчанию без ``getMe`` — уже в
``test_workers_a8.py`` (раунд 2); здесь — то, чего там нет. Проверено временной мутацией
(откачена): ``check_token=False`` в ``_main`` и ``token_check_enabled → False`` — тесты красные.

Данные выдуманные; сеть наружу не используется."""

# ruff: noqa: S603 — свой интерпретатор и свой код запуска процесса воркера

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import app.workers.notify as n
from app.config import get_settings
from app.workers.health import HEARTBEAT_ENV
from tests.test_worker_health import until
from tests.test_workers_a8 import (
    _NOTIFY_TG_LAUNCH,
    BACKEND,
    SECRET_TOKEN,
    SECRET_VALUES,
    UNREACHABLE_DB,
    _env,
    _LocalTelegram,
    _sigterm,
)

# --------------------------------------------------------------------------- связка в процессе

# (FEELINGS_ENV, FEELINGS_NOTIFY_CHECK_TOKEN или None — не задан) → ожидаемый check_token.
MATRIX = [
    ("staging", None, True),
    ("production", None, True),
    (" Staging ", None, True),  # регистр и пробелы — не повод выключить проверку
    ("prod-eu", None, True),  # неизвестное окружение — как продуктив
    ("dev", None, False),
    ("local", None, False),
    ("test", None, False),
    ("dev", "true", True),
    ("staging", "false", False),
    ("production", "true", True),
]


class _Spy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, worker: n.NotifyWorker, *, poll_s: float, stop: asyncio.Event, **kw: Any
    ) -> None:
        self.calls.append({"worker": worker, "poll_s": poll_s, "stop": stop, **kw})


@pytest.mark.parametrize(("env_name", "explicit", "expected"), MATRIX)
async def test_main_passes_token_check_flag_by_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env_name: str,
    explicit: str | None,
    expected: bool,
) -> None:
    """Б-96: ``_main`` передаёт в ``run_with_token_check`` флаг ``check_token`` — явно, по
    ``token_check_enabled(settings)``; значение по умолчанию параметра (``True``) не в счёт:
    флаг обязан прийти из ``_main`` (иначе в dev проверка включилась бы, а его потеря на стенде
    не была бы видна)."""
    monkeypatch.chdir(tmp_path)  # без чужого .env
    monkeypatch.setenv("FEELINGS_ENV", env_name)
    if explicit is None:
        monkeypatch.delenv("FEELINGS_NOTIFY_CHECK_TOKEN", raising=False)
    else:
        monkeypatch.setenv("FEELINGS_NOTIFY_CHECK_TOKEN", explicit)
    monkeypatch.setenv("FEELINGS_TELEGRAM_BOT_TOKEN", SECRET_TOKEN)
    monkeypatch.setenv("FEELINGS_WORKER_DATABASE_URL", UNREACHABLE_DB)
    monkeypatch.setenv("FEELINGS_NOTIFY_POLL_S", "0.7")
    monkeypatch.delenv("FEELINGS_WEBAPP_URL", raising=False)
    monkeypatch.setenv(HEARTBEAT_ENV, str(tmp_path / "hb.json"))
    spy = _Spy()
    monkeypatch.setattr(n, "run_with_token_check", spy)
    closed: list[n.BotApi] = []
    real_aclose = n.BotApi.aclose

    async def _aclose(self: n.BotApi) -> None:
        closed.append(self)
        await real_aclose(self)

    monkeypatch.setattr(n.BotApi, "aclose", _aclose)
    get_settings.cache_clear()
    try:
        await n._main()
    finally:
        get_settings.cache_clear()

    assert len(spy.calls) == 1, spy.calls
    call = spy.calls[0]
    assert "check_token" in call, "флаг не передан: работает значение по умолчанию"
    assert call["check_token"] is expected
    assert call["check_token"] is n.token_check_enabled(get_settings())
    assert call["poll_s"] == 0.7
    assert isinstance(call["worker"], n.NotifyWorker)
    assert closed == [call["worker"].api]  # проверяется тот же клиент, что закрывается
    get_settings.cache_clear()


# --------------------------------------------------------------------------- процесс целиком


def _spawn(
    tmp_path: Path, tg: _LocalTelegram, env_name: str, explicit: str | None
) -> subprocess.Popen[str]:
    env = _env(tmp_path, UNREACHABLE_DB) | {"FEELINGS_ENV": env_name}
    env.pop("FEELINGS_NOTIFY_CHECK_TOKEN")
    if explicit is not None:
        env["FEELINGS_NOTIFY_CHECK_TOKEN"] = explicit
    return subprocess.Popen(
        [sys.executable, "-c", _NOTIFY_TG_LAUNCH, tg.url], cwd=BACKEND, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip


async def test_staging_process_calls_getme_first_and_keeps_running(tmp_path: Path) -> None:
    """Б-96, АР-49 п.5: стенд, флаг не задан, Telegram принял токен — при старте ровно один
    ``getMe``, и это первый запрос процесса к Bot API (до любой отправки); журнал «bot token
    accepted»; процесс работает дальше, SIGTERM — код 0; токена в выводе нет (ТЗ §9.1 п.5)."""
    tg = _LocalTelegram(200)
    proc = _spawn(tmp_path, tg, "staging", None)
    try:
        await until(lambda: bool(tg.methods) or proc.poll() is not None)
        await asyncio.sleep(1.0)
        assert proc.poll() is None
    finally:
        rc, out = await _sigterm(proc)
        tg.close()
    assert rc == 0, out[-2000:]
    assert tg.methods == ["getMe"], tg.methods
    assert "notify worker: bot token accepted" in out, out[-2000:]
    assert "notify worker stopped" in out and "Traceback" not in out
    for secret in SECRET_VALUES:
        assert secret not in out, secret


@pytest.mark.parametrize(
    ("env_name", "explicit", "calls"),
    [("dev", "true", ["getMe"]), ("staging", "false", [])],
)
async def test_process_explicit_flag_overrides_env(
    tmp_path: Path, env_name: str, explicit: str, calls: list[str]
) -> None:
    """Б-96: ``FEELINGS_NOTIFY_CHECK_TOKEN`` сильнее окружения в настоящем процессе: dev + true —
    ``getMe`` при старте; staging + false — к Telegram ни одного запроса. Пульс записан —
    процесс дошёл до цикла; SIGTERM — 0."""
    tg = _LocalTelegram(200)
    proc = _spawn(tmp_path, tg, env_name, explicit)
    path = tmp_path / "hb.json"
    try:
        await until(lambda: path.exists() or proc.poll() is not None)
        await until(lambda: len(tg.methods) >= len(calls) or proc.poll() is not None)
        await asyncio.sleep(1.0)
        assert proc.poll() is None
    finally:
        rc, out = await _sigterm(proc)
        tg.close()
    assert rc == 0, out[-2000:]
    assert tg.methods == calls, tg.methods
    assert ("bot token accepted" in out) is bool(calls), out[-2000:]

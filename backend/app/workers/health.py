"""Пульс фоновых воркеров и проверка живости для health check контейнера (Б-85).

Воркер (memory, kb, pipeline, notify) после каждого **успешного** прохода цикла записывает в файл
пульса метку времени и состояние; health check контейнера (``python -m app.workers.health``)
проверяет, что метка свежая и состояние рабочее. Так видно то, чего не видно по подключению к БД:

- **зависший цикл** — метка перестаёт обновляться;
- **БД недоступна** (захват не проходит) — метка не обновляется: проход не успешный;
- **пауза из-за ошибки настройки** (модель: ``auth``/``quota``/``config``/``model_not_found``;
  бот: 401/404) — состояние ``paused``, unhealthy, хотя процесс жив;
- **ожидание при старте** (модель недоступна) — состояние ``waiting``, unhealthy, без перезапуска.

Файл — JSON ``{"ts": <unix time>, "state": "...", "reason": "..."}``: ни текстов, ни id, ни
адресов — только время, состояние и вид причины (короткий код ``[a-z0-9_]``, иначе ``other``).
Путь — ``FEELINGS_WORKER_HEARTBEAT_FILE`` (по умолчанию ``/tmp/feelings-worker-heartbeat.json``:
на стенде ``/tmp`` — tmpfs контейнера, корень только для чтения; у каждого контейнера свой
``/tmp``). Запись атомарная (временный файл + ``os.replace``); сбой записи не останавливает
воркер (health check тогда покажет устаревший пульс).

Длинная работа в последовательных воркерах (memory, kb: один захват — минуты работы модели и
разбора) — ``Heartbeat.working(limit_s)``: пульс идёт (с последним записанным состоянием), пока
работа не вышла за свою аренду; работа дольше аренды (зависла) — пульс останавливается. У
параллельных воркеров (pipeline, notify) при всех занятых слотах пульс идёт, только пока хотя бы
одна начатая задача моложе своей аренды (``any_within``): все слоты заняты зависшими задачами —
пульс устаревает (ревью р.1 MINOR-3).

Состояние ``paused`` (ошибка настройки) держится до первого вызова, доказавшего, что настройка
исправна (модель ответила; Telegram принял токен), а не только на время паузы: при пустой очереди
неверный ключ или токен не становится ``healthy`` через 5 минут (ревью р.1 MINOR-2).

Проверка (``check``): метка из будущего дальше допуска (``FUTURE_SKEW_S``) — неисправный пульс
(часы или файл испорчены); состояние печатается только из известного набора (иначе ``other``).

Модуль лёгкий (только стандартная библиотека): health check запускается каждые 30 с."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import AsyncIterator, Callable, Iterable
from pathlib import Path
from typing import Literal, cast

logger = logging.getLogger(__name__)

HEARTBEAT_ENV = "FEELINGS_WORKER_HEARTBEAT_FILE"
DEFAULT_HEARTBEAT_FILE = "/tmp/feelings-worker-heartbeat.json"  # noqa: S108 — tmpfs контейнера
DEFAULT_MAX_AGE_S = 120.0

State = Literal["starting", "ok", "waiting", "paused"]
STATES: frozenset[str] = frozenset({"starting", "ok", "waiting", "paused"})
HEALTHY: frozenset[str] = frozenset({"ok"})
FUTURE_SKEW_S = 5.0  # метка из будущего дальше этого — не свежая (ревью р.1 MINOR-5)
_REASON = re.compile(r"^[a-z0-9_]{1,32}$")


def heartbeat_path() -> Path:
    return Path(os.environ.get(HEARTBEAT_ENV) or DEFAULT_HEARTBEAT_FILE)


def _safe_reason(reason: str | None) -> str | None:
    """Только короткий код (вид ошибки) — ни текста, ни id не может попасть в файл."""
    if reason is None:
        return None
    code = reason.strip().lower()
    return code if _REASON.fullmatch(code) else "other"


class Heartbeat:
    """Файл пульса воркера. ``path=None`` — пульс выключен (тесты, разовые запуски)."""

    def __init__(self, path: str | Path | None, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path is not None else None
        self._clock = clock
        self._warned = False
        self.state: str | None = None
        self.reason: str | None = None

    @classmethod
    def from_env(cls) -> Heartbeat:
        return cls(heartbeat_path())

    def beat(self, state: State = "ok", reason: str | None = None) -> None:
        if state not in STATES:  # pragma: no cover — защита от опечатки в коде
            raise ValueError(state)
        self.state, self.reason = state, _safe_reason(reason)
        if self.path is None:
            return
        body = json.dumps({"ts": round(self._clock(), 3), "state": state, "reason": self.reason})
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            if not self._warned:  # один раз: не засорять журнал на каждом проходе
                logger.warning("worker heartbeat write failed: %s", type(exc).__name__)
                self._warned = True

    @contextlib.asynccontextmanager
    async def working(self, limit_s: float, *, interval_s: float = 15.0) -> AsyncIterator[None]:
        """Пульс во время длинной работы, но не дольше ``limit_s`` (аренда работы): работа,
        вышедшая за аренду, считается зависшей — пульс останавливается."""
        if self.path is None:
            yield
            return

        # Последнее записанное состояние (``ok`` или ``paused`` после захвата); до первого
        # прохода — ``ok``.
        known = self.state in STATES and self.state != "starting"
        state = cast(State, self.state) if known else "ok"
        reason = self.reason if known else None

        async def tick() -> None:
            end = time.monotonic() + limit_s
            while True:
                self.beat(state, reason)
                left = end - time.monotonic()
                if left <= 0:
                    return
                await asyncio.sleep(min(interval_s, left))

        task = asyncio.create_task(tick())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def any_within(started: Iterable[float], limit_s: float, *, now: float | None = None) -> bool:
    """Хотя бы одна задача (время начала по ``time.monotonic``) моложе ``limit_s`` (аренды)."""
    t = time.monotonic() if now is None else now
    return any(t - s < limit_s for s in started)


def check(
    path: Path, *, max_age_s: float = DEFAULT_MAX_AGE_S, now: float | None = None
) -> tuple[bool, str]:
    """(живой ли воркер, короткое пояснение для журнала health check — без значений)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = float(data["ts"])
        state = str(data["state"])
        reason = data.get("reason")
    except FileNotFoundError:
        return False, "no heartbeat yet"
    except (OSError, ValueError, KeyError, TypeError):
        return False, "heartbeat unreadable"
    age = (time.time() if now is None else now) - ts
    shown = state if state in STATES else "other"  # в вывод — только известное состояние
    detail = f"state={shown} reason={_safe_reason(reason) if reason else None} age={age:.0f}s"
    if state not in HEALTHY:
        return False, detail
    if age < -FUTURE_SKEW_S:
        return False, f"from the future: {detail} (skew over {FUTURE_SKEW_S:.0f}s)"
    if age > max_age_s:
        return False, f"stale: {detail} (max {max_age_s:.0f}s)"
    return True, detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.workers.health")
    parser.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_S, help="секунд")
    parser.add_argument("--file", type=Path, default=None)
    args = parser.parse_args(argv)
    ok, detail = check(args.file or heartbeat_path(), max_age_s=args.max_age)
    print(("healthy: " if ok else "unhealthy: ") + detail)  # вывод — в журнал health check
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

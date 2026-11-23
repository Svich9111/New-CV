"""Штатная остановка процессов-воркеров по SIGTERM/SIGINT (3.5, MAJOR-1).

``docker stop`` / пересоздание контейнера при выкладке шлёт SIGTERM; без обработчика Python
завершается сразу, посреди начатой работы. Здесь первый сигнал только выставляет ``stop``:
``run_forever`` воркера перестаёт захватывать новое и дожидается начатого. Повторный сигнал —
отмена главной задачи (не ждать). Тот же приём — у бота в режиме опроса (``app/bot/poll.py``).

Время на дорабатывание задаёт ``stop_grace_period`` сервиса в compose: после него Docker шлёт
SIGKILL."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Iterator

logger = logging.getLogger(__name__)

STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


@contextlib.contextmanager
def stop_on_signals(stop: asyncio.Event, *, name: str) -> Iterator[None]:
    """На время блока: SIGTERM/SIGINT → ``stop.set()``; второй сигнал — отмена текущей задачи.
    Вызывать из главной задачи работающего цикла. ``name`` — имя воркера для журнала."""
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()

    def on_signal(sig: signal.Signals) -> None:
        if stop.is_set():
            logger.warning("%s: second %s, cancelling", name, sig.name)
            if main_task is not None:
                main_task.cancel()
            return
        logger.info("%s: %s received, finishing started work", name, sig.name)
        stop.set()

    installed: list[signal.Signals] = []
    for sig in STOP_SIGNALS:
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, on_signal, sig)
            installed.append(sig)
    try:
        yield
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)

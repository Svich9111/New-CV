"""Идемпотентность по update_id: повторная доставка того же обновления не даёт второго ответа.

Хранилище — в памяти процесса с ограничением размера. Работает при одном экземпляре сервера;
при нескольких экземплярах нужен общий Redis (стек ТЗ §10) — бэклог Б-26, задачи 0.8/4.7.

Повтор, пришедший во время первой доставки, ждёт её исхода (`wait_outcome`) и отвечает так же:
если первая завершится временным сбоем, подтверждать повтор нельзя — обновление потерялось бы
(A8 1.4 D-2, ревью m2)."""

import asyncio
from collections import OrderedDict

DEFAULT_MAX_SIZE = 10_000


class UpdateDeduplicator:
    def __init__(self, max_size: int = DEFAULT_MAX_SIZE) -> None:
        self._max_size = max_size
        # update_id → None (обработка завершена) или событие (обработка ещё идёт)
        self._seen: OrderedDict[int, asyncio.Event | None] = OrderedDict()

    def claim(self, update_id: int) -> bool:
        """True — обновление новое и закреплено за вызывающим; False — уже было.
        Без await внутри: в одном цикле событий проверка и запись атомарны."""
        if update_id in self._seen:
            return False
        self._seen[update_id] = asyncio.Event()
        while len(self._seen) > self._max_size:
            _, evicted = self._seen.popitem(last=False)
            if evicted is not None:
                evicted.set()
        return True

    def in_progress(self, update_id: int) -> bool:
        """Обновление закреплено, но его обработка ещё не завершена."""
        return self._seen.get(update_id) is not None

    def complete(self, update_id: int) -> None:
        """Обработка завершена: повторные доставки подтверждаются как дубли."""
        if update_id in self._seen:
            event = self._seen[update_id]
            self._seen[update_id] = None
            if event is not None:
                event.set()

    def release(self, update_id: int) -> None:
        """Снять отметку, чтобы повторная доставка после временного сбоя была обработана."""
        event = self._seen.pop(update_id, None)
        if event is not None:
            event.set()

    async def wait_outcome(self, update_id: int) -> bool:
        """Для повтора: дождаться исхода первой доставки (срок задаёт вызывающий через
        asyncio.timeout). True — она завершена, повтор можно подтвердить; False — отметка
        снята (временный сбой) или её нет."""
        event = self._seen.get(update_id)
        if event is not None:
            await event.wait()
        return update_id in self._seen and self._seen[update_id] is None

    def __len__(self) -> int:
        return len(self._seen)

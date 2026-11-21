"""Идемпотентность по update_id: повторная доставка того же обновления не даёт второго ответа.

Хранилище — в памяти процесса с ограничением размера. Работает при одном экземпляре сервера;
при нескольких экземплярах нужен общий Redis (стек ТЗ §10) — бэклог Б-26, задачи 0.8/4.7."""

from collections import OrderedDict

DEFAULT_MAX_SIZE = 10_000


class UpdateDeduplicator:
    def __init__(self, max_size: int = DEFAULT_MAX_SIZE) -> None:
        self._max_size = max_size
        self._seen: OrderedDict[int, None] = OrderedDict()

    def claim(self, update_id: int) -> bool:
        """True — обновление новое и закреплено за вызывающим; False — уже было.
        Без await внутри: в одном цикле событий проверка и запись атомарны."""
        if update_id in self._seen:
            return False
        self._seen[update_id] = None
        while len(self._seen) > self._max_size:
            self._seen.popitem(last=False)
        return True

    def release(self, update_id: int) -> None:
        """Снять отметку, чтобы повторная доставка после временного сбоя была обработана."""
        self._seen.pop(update_id, None)

    def __len__(self) -> int:
        return len(self._seen)

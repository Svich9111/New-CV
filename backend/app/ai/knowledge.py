"""Интерфейс базы знаний по психологии (ТЗ §6.1 п.4, §6.4; наполнение — задача 2.6).

До 2.6 база пуста: ``EmptyKnowledgeBase`` ничего не находит, и блок 4 контекста пуст. Фрагменты
— справочные данные без личных сведений (таблица ``kb_chunks``); агент получает 3–5 наиболее
подходящих и не цитирует их пользователю (§6.4, промпт ``dialog``)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# §6.4: «агент получает 3–5 наиболее подходящих фрагментов».
KNOWLEDGE_LIMIT = 5


@dataclass(frozen=True)
class KnowledgeFragment:
    id: str
    text: str
    score: float = 0.0


@runtime_checkable
class KnowledgeBase(Protocol):
    async def search(
        self,
        query: str,
        *,
        vector: Sequence[float] | None,
        limit: int = KNOWLEDGE_LIMIT,
    ) -> list[KnowledgeFragment]:
        """Фрагменты по теме реплики, лучшие — первыми. ``vector`` — эмбеддинг запроса той же
        модели, что и векторы фрагментов (или ``None``)."""
        ...


class EmptyKnowledgeBase:
    """База знаний до задачи 2.6 — пусто."""

    async def search(
        self,
        query: str,
        *,
        vector: Sequence[float] | None,
        limit: int = KNOWLEDGE_LIMIT,
    ) -> list[KnowledgeFragment]:
        return []

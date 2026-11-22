"""Интерфейс базы знаний по психологии (ТЗ §6.1 п.4, §6.4; наполнение — задача 2.6).

Реализации: ``DbKnowledgeBase`` — поиск в БД функцией ``app_kb_nearest`` (миграция 0008,
АР-35); до 2.6 база пуста, поэтому и она ничего не находит. ``EmptyKnowledgeBase`` — без БД.
Фрагменты
— справочные данные без личных сведений (таблица ``kb_chunks``); агент получает 3–5 наиболее
подходящих и не цитирует их пользователю (§6.4, промпт ``dialog``)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories import knowledge as kb_repo

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


class DbKnowledgeBase:
    """База знаний в БД (``app_kb_nearest``). ``model`` — модель эмбеддинга, которой посчитан
    вектор запроса (``Embedder.model``): находятся только фрагменты с векторами той же модели.
    Без вектора — пусто (поиска по словам нет). Фильтры по меткам — подход, темы, уровень
    проблемы §6.4 (``None`` — без фильтра)."""

    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        user_id: uuid.UUID,
        model: str,
        *,
        approach: str | None = None,
        topics: Sequence[str] | None = None,
        level: int | None = None,
    ) -> None:
        self._sm = sm
        self._user_id = user_id
        self._model = model
        self._approach = approach
        self._topics = tuple(topics) if topics else None
        self._level = level

    async def search(
        self,
        query: str,
        *,
        vector: Sequence[float] | None,
        limit: int = KNOWLEDGE_LIMIT,
    ) -> list[KnowledgeFragment]:
        if vector is None or limit <= 0:
            return []
        chunks = await kb_repo.kb_nearest(
            self._sm,
            self._user_id,
            vector,
            self._model,
            limit=limit,
            approach=self._approach,
            topics=self._topics,
            level=self._level,
        )
        return [KnowledgeFragment(str(c.id), c.text, 1.0 - c.distance) for c in chunks]

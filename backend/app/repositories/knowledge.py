"""Слой доступа к базе знаний по психологии (ТЗ §6.1 п.4, §6.4; задача 2.0v, АР-35).

Поиск — только функцией БД ``app_kb_nearest`` (миграция 0008): фрагменты материалов в статусе
``indexed``, по косинусному расстоянию к вектору реплики той же модели (``embedding_model``),
с фильтрами по меткам. База знаний — справочные данные, не личные, но функция требует
пользователя (``app_uid()``), поэтому вызов — внутри ``user_scope``. Наполнение — задача 2.6;
до неё функция отдаёт пусто. Текст фрагмента идёт агенту, пользователю не цитируется (§6.4)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import to_pgvector
from app.db import sqlstate, user_scope


@dataclass(frozen=True)
class KbChunk:
    id: uuid.UUID
    document_id: uuid.UUID
    text: str
    approach: str | None
    topic_tags: tuple[str, ...]
    problem_level: int | None
    distance: float


async def kb_nearest(
    sm: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    vector: Sequence[float],
    model: str,
    *,
    limit: int,
    approach: str | None = None,
    topics: Sequence[str] | None = None,
    level: int | None = None,
) -> list[KbChunk]:
    """Ближайшие фрагменты (лучшие — первыми). Неверный уровень или вектор — 22023 из БД."""
    try:
        async with user_scope(sm, user_id) as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT id, document_id, text, approach, topic_tags, problem_level, "
                        "distance FROM app_kb_nearest(:q, :m, :n, :ap, CAST(:tp AS text[]), :lv)"
                    ),
                    {
                        "q": to_pgvector(vector),
                        "m": model,
                        "n": limit,
                        "ap": approach,
                        "tp": list(topics) if topics else None,
                        "lv": level,
                    },
                )
            ).all()
    except DBAPIError as exc:
        if sqlstate(exc) == "22023":  # вектор/модель/уровень не подошли — без базы знаний
            return []
        raise
    return [
        KbChunk(
            id=r.id if isinstance(r.id, uuid.UUID) else uuid.UUID(str(r.id)),
            document_id=r.document_id
            if isinstance(r.document_id, uuid.UUID)
            else uuid.UUID(str(r.document_id)),
            text=r.text,
            approach=r.approach,
            topic_tags=tuple(r.topic_tags or ()),
            problem_level=r.problem_level,
            distance=float(r.distance),
        )
        for r in rows
    ]

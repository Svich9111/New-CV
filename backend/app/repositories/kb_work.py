"""Слой доступа воркера индексации базы знаний (ТЗ §6.4, §10; задача 2.6; АР-39, миграция 0011).

Всё — под ``feelings_worker`` (``memory_work.worker_scope``) и только по своей аренде:
``kb_claim_index`` → ``kb_read_source`` → (разбор, метки, векторы — вне транзакций) → одна
транзакция ``kb_add_chunk`` × N, ``kb_set_chunk_embedding``, ``kb_finish_index`` — либо
``kb_fail_index(код)``. ``NULL``/``false`` функций — аренду перехватили или материал удалён:
работа бросается (``LeaseLost``), транзакция откатывается. Тексты книг в журналы не пишутся."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import to_pgvector
from app.repositories.memory_work import worker_scope


class LeaseLost(Exception):
    """Функция вернула NULL/false: аренда не наша (перехвачена, материал удалён)."""


@dataclass(frozen=True)
class IndexClaim:
    document_id: uuid.UUID
    token: uuid.UUID
    attempts: int
    source_format: str
    source_size: int


@dataclass(frozen=True)
class ChunkRow:
    text: str
    topics: tuple[str, ...]
    level: int | None
    vector: Sequence[float] | None


def _uuid(value: object) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


async def claim_index(
    sm: async_sessionmaker[AsyncSession], *, lease: timedelta, limit: int
) -> list[IndexClaim]:
    async with worker_scope(sm) as s:
        rows = (
            await s.execute(text("SELECT * FROM kb_claim_index(:l, :n)"), {"l": lease, "n": limit})
        ).all()
    return [
        IndexClaim(
            _uuid(r.document_id),
            _uuid(r.token),
            int(r.attempts),
            str(r.source_format),
            int(r.source_size),
        )
        for r in rows
    ]


async def read_source(
    sm: async_sessionmaker[AsyncSession], claim: IndexClaim
) -> tuple[bytes, str | None]:
    """(исходник, подход материала). ``LeaseLost`` — аренда не наша."""
    async with worker_scope(sm) as s:
        data = (
            await s.execute(
                text("SELECT kb_read_source(:d, :t)"), {"d": claim.document_id, "t": claim.token}
            )
        ).scalar_one_or_none()
        approach = (
            await s.execute(
                text("SELECT approach FROM kb_documents WHERE id = :d"), {"d": claim.document_id}
            )
        ).scalar_one_or_none()
    if data is None:
        raise LeaseLost
    return bytes(data), approach


async def fail(sm: async_sessionmaker[AsyncSession], claim: IndexClaim, code: str) -> bool:
    async with worker_scope(sm) as s:
        return bool(
            (
                await s.execute(
                    text("SELECT kb_fail_index(:d, :t, :r)"),
                    {"d": claim.document_id, "t": claim.token, "r": code},
                )
            ).scalar_one()
        )


async def write_index(
    sm: async_sessionmaker[AsyncSession],
    claim: IndexClaim,
    chunks: Sequence[ChunkRow],
    model: str | None,
) -> None:
    """Фрагменты, векторы и завершение — одной транзакцией (завершение последним)."""
    async with worker_scope(sm) as s:
        for ord_, ch in enumerate(chunks):
            chunk_id = (
                await s.execute(
                    text("SELECT kb_add_chunk(:d, :t, :o, :x, CAST(:tp AS text[]), :lv)"),
                    {
                        "d": claim.document_id,
                        "t": claim.token,
                        "o": ord_,
                        "x": ch.text,
                        "tp": list(ch.topics),
                        "lv": ch.level,
                    },
                )
            ).scalar_one_or_none()
            if chunk_id is None:
                raise LeaseLost
            if ch.vector is not None and model is not None:
                await s.execute(
                    text("SELECT kb_set_chunk_embedding(:c, :v, :m)"),
                    {"c": chunk_id, "v": to_pgvector(ch.vector), "m": model},
                )
        done = (
            await s.execute(
                text("SELECT kb_finish_index(:d, :t)"), {"d": claim.document_id, "t": claim.token}
            )
        ).scalar_one()
        if not done:
            raise LeaseLost

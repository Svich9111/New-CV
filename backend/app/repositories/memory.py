"""Слой доступа «Что агент помнит» (ФТ-13; ТЗ §2 п.6, §6.3; задача 2.4, этап 2; АР-36).

Чтение — в ``user_scope`` (роль ``feelings_app``, RLS ``memory_select``) **и** с явным отбором по
владельцу (``agents.user_id = :u``, живой пользователь): только свои неудалённые записи
(``deleted_at IS NULL AND text_enc IS NOT NULL``). Текст расшифровывается построчно
(``texts.open_row``): строка, которая не расшифровалась, не роняет список.

Удаление — только функцией БД ``app_delete_memory`` (SECURITY DEFINER, 0009): текст, вектор и
дата события стираются, отпечаток остаётся — запись не восстанавливается и не извлекается
повторно. Чужая, уже удалённая и несуществующая — одинаково ``False``. В журналы — ни текстов,
ни id."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import crypto
from app.core.crypto import Keyring
from app.db import sqlstate, user_scope
from app.repositories.texts import open_row

MEMORY_TYPES = ("fact", "date", "preference", "sensitive_topic", "observation", "from_partner")

_LIST = (
    "SELECT m.id, m.type::text AS type, m.source::text AS source, m.text_enc, m.event_date, "
    "m.confidence, m.created_at FROM memory_items m JOIN agents a ON a.id = m.agent_id "
    "JOIN users u ON u.id = a.user_id "
    "WHERE a.user_id = :u AND u.deleted_at IS NULL "
    "AND m.deleted_at IS NULL AND m.text_enc IS NOT NULL "
    "AND (CAST(:ty AS text) IS NULL OR m.type = CAST(:ty AS memory_type)) "
    "ORDER BY m.type, m.created_at DESC, m.id DESC"
)


class MemoryOwnerGone(Exception):
    """Владельца нет или он удалён во время запроса (42501 функции БД): для API — 401."""


@dataclass(frozen=True)
class MemoryRecord:
    id: uuid.UUID
    type: str
    source: str
    text: str | None  # None — не расшифровался (код — в ``unavailable``)
    event_date: date | None
    confidence: float
    created_at: datetime
    unavailable: str | None = None


def _uuid(value: object) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


async def list_memory(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    *,
    memory_type: str | None = None,
) -> list[MemoryRecord]:
    """Свои неудалённые записи: по категориям (порядок перечисления ``memory_type``), внутри —
    от новых к старым. ``memory_type`` — фильтр (``None`` — все)."""
    if memory_type is not None and memory_type not in MEMORY_TYPES:
        raise ValueError("memory_type")
    async with user_scope(sm, user_id) as s:
        rows = (await s.execute(text(_LIST), {"u": user_id, "ty": memory_type})).all()
    out: list[MemoryRecord] = []
    for r in rows:
        row_id = _uuid(r.id)
        body, reason = open_row(keys, crypto.MEMORY_TEXT, row_id, r.text_enc)
        out.append(
            MemoryRecord(
                row_id,
                r.type,
                r.source,
                body,
                r.event_date,
                float(r.confidence),
                r.created_at,
                reason,
            )
        )
    return out


async def delete_memory(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID, memory_id: uuid.UUID
) -> bool:
    """Удалить свою запись (``app_delete_memory``). ``False`` — чужая, удалённая или нет такой."""
    try:
        async with user_scope(sm, user_id) as s:
            done = (
                await s.execute(text("SELECT app_delete_memory(:i)"), {"i": memory_id})
            ).scalar_one()
    except DBAPIError as exc:
        if sqlstate(exc) == "42501":
            raise MemoryOwnerGone from exc
        raise
    return bool(done)

"""Слой доступа воркера уведомлений (ФТ-18; задача 3.5; ТЗ §4.4 п.3, §9.1 п.5; АР-44; Б-47).

Очередь уведомлений — доставленные сообщения без отметки ``notify_done_at`` (0013). Все вызовы —
функции БД под ``feelings_worker`` (``worker_scope``: короткая транзакция ``SET LOCAL ROLE``), прав
на таблицы у роли нет:

- **захват** — ``pipeline_claim_notify``: id сообщения, id получателя, токен аренды, номер попытки;
  текста нет. ``SKIP LOCKED`` — две копии воркера не получают одно сообщение; брошенное (аренда
  истекла) выдаётся снова. Пара распалась, получатель удалён или помечен удалённым, попытки
  исчерпаны — отметку ставит сама БД, без выдачи;
- **отметка** — ``pipeline_notify_done`` по своему токену (снимает аренду). ``False`` — аренду
  перехватили или отметка уже стоит (повтор той же записи после потерянного ответа).

Чат Telegram получателя здесь не читается: его ищет ``notify_user`` в момент отправки, в области
самого получателя (``user_scope``, RLS). Досрочного снятия аренды уведомления в БД нет
(``pipeline_release`` — только фаза ``processing``): повтор после падения — по истечении аренды."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.memory_work import worker_scope

# Интервал — через CAST из текста (asyncpg не выводит тип параметра).
_CLAIM = (
    "SELECT message_id, recipient_id, token, attempts "
    "FROM pipeline_claim_notify(CAST(CAST(:l AS text) AS interval), :n, :lim)"
)
_DONE = "SELECT pipeline_notify_done(:m, :t)"


@dataclass(frozen=True)
class NotifyClaim:
    message_id: uuid.UUID
    recipient_id: uuid.UUID
    token: uuid.UUID
    attempts: int

    def __repr__(self) -> str:  # без id в журналах и трассировках (ТЗ §9.1 п.5)
        return f"NotifyClaim(attempts={self.attempts})"


def _uuid(value: object) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


async def claim(
    sm: async_sessionmaker[AsyncSession], *, lease: timedelta, max_attempts: int, limit: int
) -> list[NotifyClaim]:
    async with worker_scope(sm) as s:
        rows = (
            await s.execute(
                text(_CLAIM),
                {"l": f"{lease.total_seconds():.3f} seconds", "n": max_attempts, "lim": limit},
            )
        ).all()
    return [
        NotifyClaim(_uuid(r.message_id), _uuid(r.recipient_id), _uuid(r.token), int(r.attempts))
        for r in rows
    ]


async def done(sm: async_sessionmaker[AsyncSession], c: NotifyClaim) -> bool:
    """Отметка «уведомлено или не нужно» по своему токену; ``False`` — не своя аренда / уже
    отмечено (ничего не изменено)."""
    async with worker_scope(sm) as s:
        value = (await s.execute(text(_DONE), {"m": c.message_id, "t": c.token})).scalar_one()
    return bool(value)

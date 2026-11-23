"""Слой доступа воркера уведомлений (ФТ-18; задача 3.5; ТЗ §4.4 п.3, §9.1 п.5; АР-44; Б-47).

Очередь уведомлений — доставленные сообщения без отметки ``notify_done_at`` (0013). Все вызовы —
функции БД под ``feelings_worker`` (``worker_scope``: короткая транзакция ``SET LOCAL ROLE``), прав
на таблицы у роли нет:

- **захват** — ``pipeline_claim_notify``: id сообщения, id получателя, токен аренды, номер попытки;
  текста нет. ``SKIP LOCKED`` — две копии воркера не получают одно сообщение; брошенное (аренда
  истекла) выдаётся снова. Пара распалась, получатель удалён или помечен удалённым, попытки
  исчерпаны — отметку ставит сама БД, без выдачи;
- **отметка** — ``pipeline_notify_done`` по своему токену (снимает аренду). ``False`` — аренду
  перехватили или отметка уже стоит (повтор той же записи после потерянного ответа);
- **снятие аренды** — ``pipeline_notify_release`` (0015) по своему токену, с отсрочкой и, по выбору,
  без расхода попытки: 429 дольше окна аренды и ошибка настройки бота (401/404) — уведомление ждёт,
  а не помечается сделанным через ``max_attempts`` пауз (ревью 3.5 р.1 MINOR-1).

Чат Telegram получателя здесь не читается: воркер ищет его в момент отправки, в области самого
получателя (``user_scope``, RLS). Повтор после падения процесса — по истечении аренды."""

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
# 0015: снятие аренды уведомления с отсрочкой (ревью 3.5 р.1 MINOR-1).
_RELEASE = "SELECT pipeline_notify_release(:m, :t, CAST(CAST(:after AS text) AS interval), :count)"
MAX_RETRY_AFTER = timedelta(days=1)  # предел pipeline_notify_release (22023 больше)


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


async def release(
    sm: async_sessionmaker[AsyncSession],
    c: NotifyClaim,
    *,
    retry_after: timedelta,
    count_attempt: bool,
) -> bool:
    """Досрочное снятие аренды уведомления по своему токену (0015, ``pipeline_notify_release``):
    захват выдаёт сообщение снова не раньше чем через ``retry_after`` (обрезается до [0, 1 день]);
    ``count_attempt=False`` — попытка не расходуется (401/404 настройки бота, 429 дольше окна
    аренды: уведомления ждут исправления, а не помечаются сделанными). ``False`` — аренда не
    своя, отметка уже стоит или сообщение удалено (ничего не изменено)."""
    after = min(max(retry_after, timedelta(0)), MAX_RETRY_AFTER)
    async with worker_scope(sm) as s:
        value = (
            await s.execute(
                text(_RELEASE),
                {
                    "m": c.message_id,
                    "t": c.token,
                    "after": f"{after.total_seconds():.3f} seconds",
                    "count": count_attempt,
                },
            )
        ).scalar_one()
    return bool(value)

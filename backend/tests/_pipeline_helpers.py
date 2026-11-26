"""Общий помощник тестов: исход конвейера входящего так, как его записывает воркер 3.2 (Б-83).

До Б-83 тесты готовили «доставленные» и «остановленные» сообщения функциями без аренды
(`pipeline_original`/`pipeline_deliver`/`pipeline_withhold`, 0001/0005); миграция 0016 их снимает
(DECISIONS.md АР-48). Теперь путь один — как у воркера (АР-44, АР-45):

1. **захват** — настоящий `pipeline_claim` под `feelings_worker` (своя короткая транзакция).
   Тестовая БД общая: чтобы захват не забрал сообщения других тестов, на время вызова все прочие
   строки `processing` держит блокировкой суперпользователь (`FOR UPDATE SKIP LOCKED`), а захват
   их пропускает (`SKIP LOCKED`) — в выдачу попадает только нужное сообщение. Распавшаяся пара и
   исчерпанные попытки завершаются самим захватом (`couple_ended`/`attempts_exhausted`);
2. **чтение** — `pipeline_read` по токену (`read_original`);
3. **завершение** — `pipeline_deliver_leased` / `pipeline_withhold_leased` (5 аргументов) по
   токену, одна транзакция на сообщение; аренда и срок (`work_lease_until`) снимаются.

Данные в тестах — выдуманные; тексты и id в журналы не пишутся."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from app.core.crypto import Keyring
from app.repositories import pipeline_work
from app.repositories.pipeline_work import DeliverResult, PipelineClaim, WithholdReason

LEASE = "5 minutes"
MAX_ATTEMPTS = 5

_HOLD_OTHERS = (
    "SELECT 1 FROM partner_messages WHERE status = 'processing' AND id <> :m FOR UPDATE SKIP LOCKED"
)
_CLAIM = (
    "SELECT message_id, token, attempts "
    "FROM pipeline_claim(CAST(CAST(:l AS text) AS interval), :n, 100)"
)
_DELIVER = (
    "SELECT pipeline_deliver_leased(:m, :t, :b, :ad, CAST(:tone AS smallint), :h, "
    "CAST(CAST(:ev AS text[]) AS safety_event_type[]))"
)
_WITHHOLD = (
    "SELECT pipeline_withhold_leased(:m, :t, CAST(:r AS varchar), "
    "CAST(CAST(:ev AS text[]) AS safety_event_type[]), CAST(CAST(:c AS text) AS safety_event_type))"
)
# Причина и тип по умолчанию для «остановлено безопасностью» (как кризис у воркера, АР-45 п.3).
SAFETY = "safety"
CRISIS = "violence"


async def claim(engine: AsyncEngine, message_id: uuid.UUID) -> PipelineClaim | None:
    """Захват одного сообщения настоящим `pipeline_claim`. `None` — захват его не выдал: оно не
    в `processing`, аренда занята, либо захват сам завершил его (пара не `active`, получателя
    нет, попытки/срок ожидания исчерпаны)."""
    async with engine.connect() as hold, hold.begin():
        await hold.execute(text(_HOLD_OTHERS), {"m": message_id})
        async with engine.connect() as conn, conn.begin():
            await conn.execute(text("SET LOCAL ROLE feelings_worker"))
            rows = (await conn.execute(text(_CLAIM), {"l": LEASE, "n": MAX_ATTEMPTS})).all()
    got = [PipelineClaim(uuid.UUID(str(r.message_id)), uuid.UUID(str(r.token)), int(r.attempts))
           for r in rows]  # fmt: skip
    if any(c.message_id != message_id for c in got):
        raise AssertionError("захват выдал чужое сообщение: удержание прочих строк не сработало")
    return got[0] if got else None


def _or_foreign(message_id: uuid.UUID, c: PipelineClaim | None) -> PipelineClaim:
    """Нет аренды — чужой (случайный) токен: завершение вернёт `lost`/`false`, как у воркера,
    чью аренду перехватили."""
    return c if c is not None else PipelineClaim(message_id, uuid.uuid4(), 0)


async def deliver_blob_leased(
    conn: AsyncConnection | AsyncSession,
    message_id: uuid.UUID,
    token: uuid.UUID,
    blob: bytes | None,
    *,
    is_adapted: bool = False,
    tone: int = 0,
    harsh: bool = False,
    events: Sequence[str] = (),
) -> Any:
    """`pipeline_deliver_leased` готовыми байтами в уже открытой транзакции под
    `feelings_worker` (гонки, проверки шифротекста). Возвращает ответ функции как есть."""
    return (
        await conn.execute(
            text(_DELIVER),
            {"m": message_id, "t": token, "b": blob, "ad": is_adapted, "tone": tone,
             "h": harsh, "ev": list(events) or None},
        )
    ).scalar_one()  # fmt: skip


async def withhold_leased(
    conn: AsyncConnection | AsyncSession,
    message_id: uuid.UUID,
    token: uuid.UUID,
    reason: str = SAFETY,
    crisis: str | None = CRISIS,
    events: Sequence[str] = (),
) -> Any:
    """`pipeline_withhold_leased` (5 аргументов) в уже открытой транзакции под
    `feelings_worker`. Тип кризиса — только у `safety`."""
    return (
        await conn.execute(
            text(_WITHHOLD),
            {"m": message_id, "t": token, "r": reason, "ev": list(events) or None,
             "c": crisis if reason == SAFETY else None},
        )
    ).scalar_one()  # fmt: skip


async def worker_call(engine: AsyncEngine, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Одна транзакция под `feelings_worker` для `deliver_blob_leased`/`withhold_leased`."""
    async with engine.connect() as conn, conn.begin():
        await conn.execute(text("SET LOCAL ROLE feelings_worker"))
        return await fn(conn, *args, **kwargs)


async def deliver_blob(
    engine: AsyncEngine,
    message_id: uuid.UUID,
    blob: bytes | None,
    *,
    is_adapted: bool = False,
    tone: int = 0,
    harsh: bool = False,
    events: Sequence[str] = (),
) -> Any:
    """Захват → доставка байтами (`delivered` / `couple_ended` / `lost`)."""
    c = _or_foreign(message_id, await claim(engine, message_id))
    return await worker_call(
        engine, deliver_blob_leased, message_id, c.token, blob,
        is_adapted=is_adapted, tone=tone, harsh=harsh, events=events,
    )  # fmt: skip


async def read_original(engine: AsyncEngine, keys: Keyring, c: PipelineClaim) -> str | None:
    """Одобренный оригинал по аренде — слой доступа воркера (`pipeline_read`)."""
    return await pipeline_work.read_original(async_sessionmaker(engine), keys, c)


async def deliver_text(
    engine: AsyncEngine,
    keys: Keyring,
    message_id: uuid.UUID,
    for_recipient: str,
    *,
    is_adapted: bool = False,
    tone: int = 0,
    harsh: bool = False,
    events: Sequence[str] = (),
    c: PipelineClaim | None = None,
) -> DeliverResult:
    """Захват (если аренда не передана) → доставка слоем доступа воркера
    (`pipeline_work.deliver`: шифрование версии для получателя с контекстом сообщения)."""
    if c is None:
        c = await claim(engine, message_id)
    return await pipeline_work.deliver(
        async_sessionmaker(engine), keys, _or_foreign(message_id, c), for_recipient,
        is_adapted=is_adapted, tone=tone, harsh=harsh, events=events,
    )  # fmt: skip


async def withhold(
    engine: AsyncEngine,
    message_id: uuid.UUID,
    reason: WithholdReason = "safety",
    crisis: str | None = CRISIS,
    events: Sequence[str] = (),
) -> bool:
    """Захват → остановка слоем доступа воркера (`pipeline_work.withhold`). `False` — захват не
    выдал сообщение (не в конвейере) или аренду перехватили."""
    c = _or_foreign(message_id, await claim(engine, message_id))
    return await pipeline_work.withhold(
        async_sessionmaker(engine), c, reason, events,
        crisis=crisis if reason == "safety" else None,
    )  # fmt: skip

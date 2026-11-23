"""Слой доступа воркера конвейера входящего (ТЗ §6.2 п.3–7, ФТ-09; задача 3.2; АР-44).

Все вызовы — функции БД под ``feelings_worker`` (``worker_scope``: ``SET LOCAL ROLE``), прав на
таблицы у роли нет. Транзакции (АР-44, Б-45):

- **захват** — своя короткая транзакция (``pipeline_claim``: id, токен, номер попытки; ``SKIP
  LOCKED`` — две копии воркера не получают одно сообщение; брошенное с истёкшей арендой
  выдаётся снова; пара распалась / попытки исчерпаны — завершает сама БД);
- **чтение** — отдельная транзакция (``pipeline_read``: шифротекст и id черновика только по своей
  аренде и при черновике ``sent``, Б-39, Б-48); расшифровка — с контекстом черновика;
- **завершение** — одна транзакция на одно сообщение (``pipeline_deliver_leased`` или
  ``pipeline_withhold_leased``): автор → пара → сообщение (порядок блокировок — в БД).

Модель вызывается вне транзакций (воркер). Версия для получателя шифруется здесь с контекстом
``partner_messages|adapted_enc|<id сообщения>``; события безопасности — только типом от имени
автора (ТЗ §9.3). Тексты и id в журналы не пишутся."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import crypto
from app.core.crypto import Keyring
from app.repositories import texts
from app.repositories.chat import SAFETY_EVENT_TYPES
from app.repositories.memory_work import worker_scope

logger = logging.getLogger(__name__)

WithholdReason = Literal["safety", "model_refusal", "pipeline_error"]
DeliverResult = Literal["delivered", "couple_ended", "lost"]

# Интервал и массив перечисления — через CAST из текста (asyncpg не выводит тип параметра).
_CLAIM = (
    "SELECT message_id, token, attempts "
    "FROM pipeline_claim(CAST(CAST(:l AS text) AS interval), :n, :lim)"
)
_DELIVER = (
    "SELECT pipeline_deliver_leased(:m, :t, :b, :ad, CAST(:tone AS smallint), :h, "
    "CAST(CAST(:ev AS text[]) AS safety_event_type[]))"
)
# 0014: 5 аргументов — тип кризиса для автора (``safety`` требует его, прочие причины — NULL).
_WITHHOLD = (
    "SELECT pipeline_withhold_leased(:m, :t, :r, "
    "CAST(CAST(:ev AS text[]) AS safety_event_type[]), "
    "CAST(CAST(:cr AS text) AS safety_event_type))"
)
_RELEASE = "SELECT pipeline_release(:m, :t, CAST(CAST(:after AS text) AS interval), :count)"
MAX_RETRY_AFTER = timedelta(days=1)  # предел pipeline_release (22023 больше)


@dataclass(frozen=True)
class PipelineClaim:
    message_id: uuid.UUID
    token: uuid.UUID
    attempts: int


def _uuid(value: object) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _interval(value: timedelta) -> str:
    return f"{value.total_seconds():.3f} seconds"


_DB_EVENT_TYPES = frozenset(SAFETY_EVENT_TYPES.values())


def _db_type(e: str | None) -> str | None:
    """Тип ``crisis`` или уже значение перечисления → значение ``safety_event_type``."""
    if e in SAFETY_EVENT_TYPES:
        return SAFETY_EVENT_TYPES[e]
    return e if e in _DB_EVENT_TYPES else None


def event_types(events: Iterable[str]) -> list[str]:
    """Типы ``crisis`` → значения ``safety_event_type`` (``outgoing_threat`` →
    ``threat_outgoing``). Неизвестный тип отбрасывается с предупреждением (только код типа, без
    текста): остановка по безопасности не должна срываться из-за события (ревью 3.2 MINOR-6)."""
    out = []
    for e in events:
        db = _db_type(e)
        if db is not None:
            out.append(db)
        else:
            logger.warning("pipeline: unknown safety event type dropped")
    return out


def crisis_type(primary: str | None, events: Iterable[str]) -> str:
    """Тип кризиса для автора (АР-45 п.3; шаблон отправителя при ``threat_outgoing``, §9.3):
    главный тип, иначе первый известный не-``underage`` из событий. Неизвестно ничего —
    ``life_threat`` с предупреждением: самый осторожный протокол (экстренные службы), остановка
    не срывается (MINOR-6; при ``MESSAGE_CRISIS`` ⊂ ``SAFETY_EVENT_TYPES`` недостижимо)."""
    for e in (primary, *events):
        db = _db_type(e)
        if db is not None and db != "underage":
            return db
    logger.warning("pipeline: crisis type unknown, cautious protocol used")
    return "life_threat"


async def claim(
    sm: async_sessionmaker[AsyncSession], *, lease: timedelta, max_attempts: int, limit: int
) -> list[PipelineClaim]:
    async with worker_scope(sm) as s:
        rows = (
            await s.execute(text(_CLAIM), {"l": _interval(lease), "n": max_attempts, "lim": limit})
        ).all()
    return [PipelineClaim(_uuid(r.message_id), _uuid(r.token), int(r.attempts)) for r in rows]


async def read_original(
    sm: async_sessionmaker[AsyncSession], keys: Keyring, c: PipelineClaim
) -> str | None:
    """Одобренный оригинал по своей аренде; ``None`` — одобрения нет (не обрабатывать, Б-48).
    ``DecryptionError`` — шифротекст не открылся (неустранимо)."""
    async with worker_scope(sm) as s:
        return await texts.pipeline_read(s, keys, c.message_id, c.token)


async def deliver(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    c: PipelineClaim,
    for_recipient: str,
    *,
    is_adapted: bool,
    tone: int,
    harsh: bool,
    events: Sequence[str] = (),
) -> DeliverResult:
    """Доставка по токену: ``delivered``; ``couple_ended`` — пара распалась или получатель удалил
    аккаунт, пока шла модель: не доставлено (``withheld/couple_ended``; причина хранится в
    строке, но в ленте автора её не видно — лента только текущей активной пары, ТЗ §4.5 п.2);
    ``lost`` — аренду перехватили, сообщения нет (удалено с аккаунтом автора) или исход уже
    записан — ничего не изменено."""
    blob = keys.seal(crypto.MESSAGE_ADAPTED, c.message_id, for_recipient)
    async with worker_scope(sm) as s:
        value = (
            await s.execute(
                text(_DELIVER),
                {
                    "m": c.message_id,
                    "t": c.token,
                    "b": blob,
                    "ad": is_adapted,
                    "tone": tone,
                    "h": harsh,
                    "ev": event_types(events) or None,
                },
            )
        ).scalar_one()
    if value not in ("delivered", "couple_ended", "lost"):
        raise RuntimeError("неожиданный ответ pipeline_deliver_leased")
    result: DeliverResult = value
    return result


async def withhold(
    sm: async_sessionmaker[AsyncSession],
    c: PipelineClaim,
    reason: WithholdReason,
    events: Sequence[str] = (),
    crisis: str | None = None,
) -> bool:
    """Остановка по токену с причиной (видна автору в ленте); ``False`` — как ``lost`` (аренду
    перехватили, сообщения нет или исход уже записан — запись по токену идемпотентна, 0014).
    ``crisis`` — тип кризиса (crisis-тип или значение перечисления) только при ``safety``; не
    задан — по событиям (``crisis_type``)."""
    if reason != "safety" and crisis is not None:
        raise ValueError("тип кризиса — только у остановки безопасностью")
    cr = crisis_type(crisis, events) if reason == "safety" else None
    async with worker_scope(sm) as s:
        value = (
            await s.execute(
                text(_WITHHOLD),
                {
                    "m": c.message_id,
                    "t": c.token,
                    "r": reason,
                    "ev": event_types(events) or None,
                    "cr": cr,
                },
            )
        ).scalar_one()
    return bool(value)


async def release(
    sm: async_sessionmaker[AsyncSession],
    c: PipelineClaim,
    *,
    retry_after: timedelta,
    count_attempt: bool,
) -> bool:
    """Досрочное снятие аренды (0014, АР-45): сообщение снова выдаётся захватом не раньше чем
    через ``retry_after``; ``count_attempt=False`` — попытка не расходуется (пауза при ошибке
    настройки модели; предел ожидания — 24 ч от отправки, ставит БД). ``False`` — аренда не
    своя или сообщение уже не в конвейере."""
    after = min(max(retry_after, timedelta(0)), MAX_RETRY_AFTER)
    async with worker_scope(sm) as s:
        value = (
            await s.execute(
                text(_RELEASE),
                {
                    "m": c.message_id,
                    "t": c.token,
                    "after": _interval(after),
                    "count": count_attempt,
                },
            )
        ).scalar_one()
    return bool(value)

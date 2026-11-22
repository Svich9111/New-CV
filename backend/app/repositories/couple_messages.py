"""Слой доступа ленты пары (ФТ-10, ФТ-11; ТЗ §2 п.3, §4.4 п.3–4, §6.2 п.7, §7; задача 3.3;
АР-11, АР-25).

Всё — в ``user_scope`` (роль ``feelings_app``, RLS ``pm_select``) через функции БД:
``app_couple_feed`` (лента: автору — его оригинал, получателю — версия для получателя;
недоставленное получателю не видно), ``app_read_original`` (оригинал; получателю — с фиксацией
просмотра ``original_viewed_at``), ``app_mark_read`` (прочитано — только получатель
доставленного). Оригинала (``original_enc``) приложение напрямую не читает (АР-11); версию для
получателя своего отправленного сообщения автор не получает (АР-25 п.3).

**Только текущая пара**: ``app_couple_feed`` отдаёт сообщения всех пар пользователя, поэтому
строки фильтруются по текущей активной паре (``couple_members.active`` и ``couples.status =
active``); после выхода из пары лента пуста, оригинал и «прочитано» — 404 (обмен прекращается,
ТЗ §4.5 п.2). Предложение для A1 — фильтр пары и порядок ``(sent_at, id)`` в самой функции.

Тексты расшифровываются построчно (``texts.open_row``); в журналы — ни текстов, ни id."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import crypto
from app.core.crypto import Keyring
from app.db import user_scope
from app.repositories.texts import open_row

FEED_BATCH = 100  # предел одного вызова app_couple_feed (LEAST(..., 100))
MAX_BATCHES = 50  # не больше 5000 просмотренных строк на страницу
DELIVERED = ("delivered", "read", "answered")

_FEED = (
    "SELECT f.id, f.couple_id, f.outgoing, f.text_enc, f.is_adapted, f.harsh_tone_warning, "
    "f.status::text AS status, f.reply_to_id, f.sent_at, f.read_at, pm.draft_id "
    "FROM app_couple_feed(:l, :b) f LEFT JOIN partner_messages pm ON pm.id = f.id"
)
_CURRENT_COUPLE = (
    "SELECT c.id FROM couple_members cm JOIN couples c ON c.id = cm.couple_id "
    "WHERE cm.user_id = :u AND cm.active AND c.status = 'active'"
)
_MESSAGE = (
    "SELECT couple_id, author_id, recipient_id, status::text AS status, draft_id "
    "FROM partner_messages WHERE id = :m"
)


@dataclass(frozen=True)
class FeedPosition:
    sent_at: datetime
    id: uuid.UUID


@dataclass(frozen=True)
class FeedItem:
    id: uuid.UUID
    outgoing: bool
    text: str | None  # None — не расшифровался (код — в ``unavailable``)
    is_adapted: bool
    harsh_tone_warning: bool
    status: str
    reply_to_id: uuid.UUID | None
    sent_at: datetime
    read_at: datetime | None
    unavailable: str | None = None


class Gone(Exception):
    """Сообщения нет среди видимых пользователю в текущей паре (чужое, недоставленное, бывшей
    пары): для API — 404, неразличимо."""


def _uuid(value: object) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


async def _current_couple(s: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    value = (await s.execute(text(_CURRENT_COUPLE), {"u": user_id})).scalar_one_or_none()
    return None if value is None else _uuid(value)


def _open(keys: Keyring, r: Any) -> tuple[str | None, str | None]:
    m = r._mapping
    msg_id = _uuid(m["id"])
    if m["text_enc"] is None:
        return None, crypto.CONTEXT_MISSING
    if not m["outgoing"]:
        return open_row(keys, crypto.MESSAGE_ADAPTED, msg_id, m["text_enc"])
    draft = None if m["draft_id"] is None else _uuid(m["draft_id"])
    return open_row(keys, crypto.MESSAGE_ORIGINAL, draft, m["text_enc"])


async def read_page(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    *,
    limit: int,
    before: FeedPosition | None = None,
) -> tuple[list[FeedItem], FeedPosition | None]:
    """Страница ленты текущей пары от новых к старым (``sent_at DESC, id DESC``), строго старше
    ``before``. → (сообщения, позиция для следующей страницы или ``None`` — дальше нет).
    Нерасшифрованное сообщение не пропускается: ``text = None``, код — в ``unavailable``."""
    if not 1 <= limit <= FEED_BATCH:
        raise ValueError("limit")
    items: list[FeedItem] = []
    key = (before.sent_at, before.id) if before else None
    async with user_scope(sm, user_id) as s:
        couple = await _current_couple(s, user_id)
        if couple is None:
            return [], None
        for _ in range(MAX_BATCHES):
            # Функция отбирает строго «раньше p_before»; +1 мкс и отбор по ключу ниже не теряют
            # сообщений с тем же временем, что у последнего показанного.
            bound = key[0] + timedelta(microseconds=1) if key else None
            rows = (await s.execute(text(_FEED), {"l": FEED_BATCH, "b": bound})).all()
            ordered = sorted(rows, key=lambda r: (r.sent_at, _uuid(r.id)), reverse=True)
            progressed = False
            for r in ordered:
                rk = (r.sent_at, _uuid(r.id))
                if key is not None and rk >= key:
                    continue
                key, progressed = rk, True
                if _uuid(r.couple_id) != couple:
                    continue
                if len(items) == limit:
                    last = items[-1]
                    return items, FeedPosition(last.sent_at, last.id)
                body, reason = _open(keys, r)
                items.append(
                    FeedItem(
                        id=rk[1],
                        outgoing=bool(r.outgoing),
                        text=body,
                        is_adapted=bool(r.is_adapted),
                        harsh_tone_warning=bool(r.harsh_tone_warning),
                        status=r.status,
                        reply_to_id=None if r.reply_to_id is None else _uuid(r.reply_to_id),
                        sent_at=r.sent_at,
                        read_at=r.read_at,
                        unavailable=reason,
                    )
                )
            if len(rows) < FEED_BATCH or not progressed:
                break
    return items, None


async def _visible(s: AsyncSession, user_id: uuid.UUID, message_id: uuid.UUID) -> Any:
    """Строка сообщения, если оно видно пользователю (RLS ``pm_select``) и из текущей пары."""
    row = (await s.execute(text(_MESSAGE), {"m": message_id})).one_or_none()
    couple = await _current_couple(s, user_id)
    if row is None or couple is None or _uuid(row.couple_id) != couple:
        raise Gone
    return row


async def read_original(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    message_id: uuid.UUID,
) -> str:
    """Оригинал (ФТ-10): получателю доставленного — с фиксацией просмотра (``app_read_original``),
    автору — свой текст без фиксации. Расшифровка — в той же транзакции: не удалась —
    ``crypto.DecryptionError`` откатывает и отметку (просмотр засчитывается, только когда текст
    показан, ТЗ §7). ``Gone`` — 404."""
    async with user_scope(sm, user_id) as s:
        row: Any = await _visible(s, user_id, message_id)
        blob = (
            await s.execute(text("SELECT app_read_original(:m)"), {"m": message_id})
        ).scalar_one()
        if blob is None:
            raise Gone
        draft = row.draft_id
        if draft is None:
            raise crypto.DecryptionError(crypto.CONTEXT_MISSING)
        return keys.open(crypto.MESSAGE_ORIGINAL, _uuid(draft), blob)


async def mark_read(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID, message_id: uuid.UUID
) -> None:
    """«Прочитано» (``app_mark_read``) — получатель доставленного сообщения текущей пары.
    Уже прочитанное или отвеченное — без изменений (повтор безопасен). ``Gone`` — 404 (своё
    отправленное, чужое, недоставленное, бывшей пары)."""
    async with user_scope(sm, user_id) as s:
        row: Any = await _visible(s, user_id, message_id)
        if row.recipient_id is None or _uuid(row.recipient_id) != user_id:
            raise Gone
        if row.status not in DELIVERED:
            raise Gone
        await s.execute(text("SELECT app_mark_read(:m)"), {"m": message_id})

"""Слой доступа ленты пары (ФТ-10, ФТ-11; ТЗ §2 п.3, §4.4 п.3–4, §6.2 п.7, §7; задача 3.3;
АР-11, АР-25, АР-40).

Всё — в ``user_scope`` (роль ``feelings_app``, RLS ``pm_select``) через функции БД:

- ``app_couple_feed_page`` (0012) — лента **только текущей активной пары**, порядок
  ``(sent_at DESC, id DESC)``, курсор — ключ последней показанной строки; автору — его оригинал,
  получателю — версия для получателя; ``processing``/``withheld`` получателю не видны. После
  выхода из пары или её распада лента пуста (ТЗ §4.5 п.2). Предел функции — 100 строк: при
  ``limit = 100`` полная страница даёт курсор, следующая может оказаться пустой (Б-80). Автору у
  остановленного (``withheld``) — причина ``withheld_reason``, тип кризиса ``withheld_crisis``
  и набор признаков (0013/0014/0021, АР-44, АР-45; получателю функция отдаёт NULL); по набору —
  имена блоков протокола §9.3 ``withheld_crisis_blocks`` (``crisis.plan``, как в конвейере);
- ``app_read_original`` — оригинал: получателю доставленного — с фиксацией просмотра
  ``original_viewed_at``, автору — без неё; сообщение должно быть из текущей пары (иначе ``Gone``
  — 404, как в ленте). Не расшифровался — транзакция откатывается вместе с отметкой (просмотр
  засчитывается, только когда текст показан), API отдаёт ``UnavailableText``;
- ``app_mark_read`` — «прочитано», только получатель доставленного сообщения текущей пары.

Оригинала (``original_enc``) приложение напрямую не читает (АР-11); версию для получателя своего
отправленного сообщения автор не получает (АР-25 п.3). Тексты расшифровываются построчно
(``texts.open_row``: нерасшифрованное не пропускается — код причины); в журналы — ни текстов, ни
id."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import crisis
from app.core import crypto
from app.core.crypto import Keyring
from app.db import user_scope
from app.repositories.chat import SAFETY_EVENT_TYPES
from app.repositories.texts import open_row

FEED_BATCH = 100  # предел одного вызова app_couple_feed (LEAST(..., 100))
DELIVERED = ("delivered", "read", "answered")

_FEED_PAGE = (
    "SELECT id, couple_id, outgoing, text_enc, is_adapted, harsh_tone_warning, "
    "status::text AS status, reply_to_id, draft_id, sent_at, read_at, "
    "withheld_reason, withheld_crisis::text AS withheld_crisis, "
    "withheld_crisis_types::text[] AS withheld_crisis_types "
    "FROM app_couple_feed_page(:l, :ba, :bi)"
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
    # Только автору и только у ``withheld`` (0013/0014: функция отдаёт их лишь автору; АР-44,
    # АР-45): причина остановки и тип кризиса — какой протокол помощи показать (§9.3).
    withheld_reason: str | None = None
    withheld_crisis: str | None = None
    # 0021 (АР-45, дополнение 3.4): имена блоков протокола §9.3 в порядке показа — ровно
    # ``crisis.plan`` по набору признаков и главному типу; ``None`` — набор неизвестен (до 0021).
    withheld_crisis_blocks: tuple[str, ...] | None = None


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


# Значение ``safety_event_type`` → строка §9.3 (``crisis``); обратное ``SAFETY_EVENT_TYPES``.
_CRISIS_OF_DB = {db: t for t, db in SAFETY_EVENT_TYPES.items()}


def crisis_blocks(primary: str | None, types: Sequence[str] | None) -> tuple[str, ...] | None:
    """Имена блоков протокола автору остановленного сообщения — тот же ``crisis.plan``, что
    строит ответ ``crisis.respond`` в конвейере (``withheld=True``), по набору признаков и
    главному типу из БД. ``None`` — набора нет (до 0021) или он не читается: клиент показывает
    консервативный набор. Только имена — текста сообщения здесь нет."""
    if primary is None or not types:
        return None
    found = [_CRISIS_OF_DB[t] for t in types if t in _CRISIS_OF_DB and t != "underage"]
    main = _CRISIS_OF_DB.get(primary)
    if main is None or main == "underage" or main not in found:
        return None
    return crisis.plan(found, primary=main, withheld=True).blocks


def _withheld(r: Any) -> dict[str, str | None]:
    """Причина и тип кризиса — только своему ``withheld`` (функция уже обнуляет их получателю;
    здесь — вторая граница: у входящих и не остановленных полей нет)."""
    if not r.outgoing or r.status != "withheld":
        return {"withheld_reason": None, "withheld_crisis": None}
    reason = r.withheld_reason
    return {
        "withheld_reason": reason,
        "withheld_crisis": r.withheld_crisis if reason == "safety" else None,
    }


def _blocks(r: Any) -> tuple[str, ...] | None:
    """Блоки протокола — только своему ``withheld`` по безопасности (та же вторая граница)."""
    if not r.outgoing or r.status != "withheld" or r.withheld_reason != "safety":
        return None
    return crisis_blocks(r.withheld_crisis, r.withheld_crisis_types)


async def read_page(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    *,
    limit: int,
    before: FeedPosition | None = None,
) -> tuple[list[FeedItem], FeedPosition | None]:
    """Страница ленты текущей пары от новых к старым (``app_couple_feed_page``, 0012: порядок
    ``(sent_at DESC, id DESC)``, курсор — ключ последней показанной строки, только текущая
    активная пара). → (сообщения, позиция для следующей страницы или ``None``). Нерасшифрованное
    не пропускается: ``text = None``, код — в ``unavailable``."""
    if not 1 <= limit <= FEED_BATCH:
        raise ValueError("limit")
    ask = min(limit + 1, FEED_BATCH)
    async with user_scope(sm, user_id) as s:
        rows = (
            await s.execute(
                text(_FEED_PAGE),
                {
                    "l": ask,
                    "ba": before.sent_at if before else None,
                    "bi": before.id if before else None,
                },
            )
        ).all()
    items: list[FeedItem] = []
    for r in rows[:limit]:
        body, reason = _open(keys, r)
        items.append(
            FeedItem(
                id=_uuid(r.id),
                outgoing=bool(r.outgoing),
                text=body,
                is_adapted=bool(r.is_adapted),
                harsh_tone_warning=bool(r.harsh_tone_warning),
                status=r.status,
                reply_to_id=None if r.reply_to_id is None else _uuid(r.reply_to_id),
                sent_at=r.sent_at,
                read_at=r.read_at,
                unavailable=reason,
                **_withheld(r),
                withheld_crisis_blocks=_blocks(r),
            )
        )
    # Предел функции — 100: при limit = 100 полная страница означает «возможно, есть ещё»
    # (следующая может оказаться пустой).
    more = len(rows) > limit or (len(rows) == FEED_BATCH == limit)
    if not more or not items:
        return items, None
    return items, FeedPosition(items[-1].sent_at, items[-1].id)


async def _visible(s: AsyncSession, user_id: uuid.UUID, message_id: uuid.UUID) -> Any:
    """Строка сообщения, если оно видно пользователю (RLS ``pm_select``) и из текущей пары."""
    row = (await s.execute(text(_MESSAGE), {"m": message_id})).one_or_none()
    couple = await _current_couple(s, user_id)
    if row is None or couple is None or _uuid(row.couple_id) != couple:
        raise Gone
    return row


class _Undecryptable(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


async def read_original(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    message_id: uuid.UUID,
) -> tuple[str | None, str | None]:
    """Оригинал (ФТ-10): получателю доставленного — с фиксацией просмотра (``app_read_original``),
    автору — свой текст без фиксации. → (текст, None) или (None, код причины): расшифровка — в
    той же транзакции, не удалась — транзакция откатывается вместе с отметкой (просмотр
    засчитывается, только когда текст показан, ТЗ §7; АР-40). ``Gone`` — 404."""
    try:
        async with user_scope(sm, user_id) as s:
            row: Any = await _visible(s, user_id, message_id)
            blob = (
                await s.execute(text("SELECT app_read_original(:m)"), {"m": message_id})
            ).scalar_one()
            if blob is None:
                raise Gone
            body, reason = open_row(
                keys,
                crypto.MESSAGE_ORIGINAL,
                None if row.draft_id is None else _uuid(row.draft_id),
                blob,
            )
            if reason is not None:
                raise _Undecryptable(reason)  # откат: отметка просмотра не сохраняется
            return body, None
    except _Undecryptable as exc:
        return None, exc.reason


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

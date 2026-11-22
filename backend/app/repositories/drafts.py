"""Слой доступа черновиков для API (ФТ-07, ФТ-08; ТЗ §2 п.2, §6.2 п.1–2; задача 3.1, A2; АР-11,
АР-40; Б-38).

Всё — в ``user_scope`` (роль ``feelings_app``, RLS ``drafts_*`` — только черновики своего агента).
Правка и отказ — проверка статуса и версии и запись **одной транзакцией** со строкой черновика
``FOR UPDATE`` (одновременная правка и «Отправить» не теряют друг друга: вторая видит новую
версию или статус ``sent``). «Отправить» — только функцией БД ``app_send_draft`` (единственный
путь сообщения к партнёру, ФТ-08; её коды FA001…FA004 и 42501 сопоставляются здесь). Тексты в
журналы не пишутся."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import crypto
from app.core.crypto import Keyring
from app.db import sqlstate, user_scope
from app.repositories.texts import open_row

_DRAFT = (
    "SELECT md.id, md.text_enc, md.version, md.status::text AS status, md.reply_to_id "
    "FROM message_drafts md JOIN agents a ON a.id = md.agent_id "
    "JOIN users u ON u.id = a.user_id "
    "WHERE md.id = :d AND a.user_id = :u AND u.deleted_at IS NULL"
)
_ACTIVE_COUPLE = (
    "SELECT EXISTS (SELECT 1 FROM couple_members me JOIN couples c ON c.id = me.couple_id "
    "AND c.status = 'active' JOIN couple_members other ON other.couple_id = c.id "
    "AND other.active AND other.user_id <> me.user_id WHERE me.user_id = :u AND me.active)"
)
_SENT = (
    "SELECT id, is_adapted, harsh_tone_warning, status::text AS status, reply_to_id, sent_at, "
    "read_at FROM partner_messages WHERE id = :m"
)


class DraftNotFound(Exception):
    """Нет среди своих (404, неразличимо)."""


class NotEditable(Exception):
    """Статус не позволяет действие (409 ``not_editable``)."""


class VersionConflict(Exception):
    """``expected_version`` не текущая (409 ``version_conflict``)."""


class Blocked(Exception):
    """Черновик остановлен проверкой безопасности (422 ``blocked_by_safety``)."""


class CoupleNotActive(Exception):
    """Нет активной пары или партнёр вышел/удалён (409 ``couple_not_active``)."""


class BadReplyTarget(Exception):
    """Ответ не на своё доставленное входящее текущей пары (422 ``bad_reply_target``)."""


class OwnerGone(Exception):
    """Владелец удалён во время запроса (401)."""


@dataclass(frozen=True)
class DraftRow:
    id: uuid.UUID
    text: str | None  # None — не расшифровался (код — в ``unavailable``)
    version: int
    status: str
    reply_to_id: uuid.UUID | None
    unavailable: str | None = None


@dataclass(frozen=True)
class SentMessage:
    id: uuid.UUID
    text: str | None
    is_adapted: bool
    harsh_tone_warning: bool
    status: str
    reply_to_id: uuid.UUID | None
    sent_at: datetime
    read_at: datetime | None
    unavailable: str | None = None


def _uuid(value: object) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _row(keys: Keyring, r: Any) -> DraftRow:
    draft_id = _uuid(r.id)
    body, reason = open_row(keys, crypto.DRAFT_TEXT, draft_id, r.text_enc)
    return DraftRow(
        draft_id,
        body,
        int(r.version),
        r.status,
        None if r.reply_to_id is None else _uuid(r.reply_to_id),
        reason,
    )


async def _locked(s: AsyncSession, user_id: uuid.UUID, draft_id: uuid.UUID) -> Any:
    """Строка своего черновика ``FOR UPDATE``. Отправленный и остановленный черновик RLS для
    изменения не отдаёт (``drafts_update``, Б-38) — тогда он ``NotEditable``, а не 404."""
    params = {"d": draft_id, "u": user_id}
    row = (await s.execute(text(_DRAFT + " FOR UPDATE OF md"), params)).one_or_none()
    if row is not None:
        return row
    if (await s.execute(text(_DRAFT), params)).one_or_none() is None:
        raise DraftNotFound
    raise NotEditable


async def has_active_couple(sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> bool:
    """Есть активная пара с активным партнёром — черновику есть кому уйти."""
    async with user_scope(sm, user_id) as s:
        return bool((await s.execute(text(_ACTIVE_COUPLE), {"u": user_id})).scalar_one())


async def get(
    sm: async_sessionmaker[AsyncSession], keys: Keyring, user_id: uuid.UUID, draft_id: uuid.UUID
) -> DraftRow:
    async with user_scope(sm, user_id) as s:
        row = (await s.execute(text(_DRAFT), {"d": draft_id, "u": user_id})).one_or_none()
    if row is None:
        raise DraftNotFound
    return _row(keys, row)


async def edit(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    draft_id: uuid.UUID,
    body: str,
    *,
    expected_version: int,
) -> DraftRow:
    """Правка автором: только ``draft`` (иначе ``NotEditable``) и только увиденной версии (иначе
    ``VersionConflict``); версия +1, текст как есть."""
    async with user_scope(sm, user_id) as s:
        row = await _locked(s, user_id, draft_id)
        if row.status != "draft":
            raise NotEditable
        if int(row.version) != expected_version:
            raise VersionConflict
        version = (
            await s.execute(
                text(
                    "UPDATE message_drafts SET text_enc = :t, version = version + 1, "
                    "updated_at = now() WHERE id = :d RETURNING version"
                ),
                {"d": draft_id, "t": keys.seal(crypto.DRAFT_TEXT, draft_id, body)},
            )
        ).scalar_one()
    return DraftRow(
        draft_id,
        body,
        int(version),
        "draft",
        None if row.reply_to_id is None else _uuid(row.reply_to_id),
    )


async def reject(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID, draft_id: uuid.UUID
) -> None:
    """«Не отправлять»: ``draft`` → ``rejected``; уже отклонённый — без изменений; иначе
    ``NotEditable``."""
    async with user_scope(sm, user_id) as s:
        row = await _locked(s, user_id, draft_id)
        if row.status == "rejected":
            return
        if row.status != "draft":
            raise NotEditable
        await s.execute(
            text("UPDATE message_drafts SET status = 'rejected', updated_at = now() WHERE id = :d"),
            {"d": draft_id},
        )


async def send(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    draft_id: uuid.UUID,
    *,
    expected_version: int,
) -> SentMessage:
    """«Отправить» (ФТ-08): ``app_send_draft`` — оригинал (ТЗ §6.2 п.2) = текст этой версии,
    сообщение ``processing``, черновик ``sent``. Предпроверка (без блокировки — порядок блокировок
    АР-13 задаёт функция) различает 404 / ``blocked`` / ``not_editable`` / версию; функция
    перепроверяет всё под своими блокировками."""
    try:
        async with user_scope(sm, user_id) as s:
            row = (await s.execute(text(_DRAFT), {"d": draft_id, "u": user_id})).one_or_none()
            if row is None:
                raise DraftNotFound
            if row.status == "blocked":
                raise Blocked
            if row.status not in ("draft", "approved"):
                raise NotEditable
            if int(row.version) != expected_version:
                raise VersionConflict
            msg_id = (
                await s.execute(
                    text("SELECT app_send_draft(:d, :v)"), {"d": draft_id, "v": expected_version}
                )
            ).scalar_one()
            m = (await s.execute(text(_SENT), {"m": msg_id})).one()
            body, reason = open_row(keys, crypto.DRAFT_TEXT, draft_id, row.text_enc)
    except DBAPIError as exc:
        code = sqlstate(exc)
        if code == "FA001":
            raise NotEditable from exc  # отправлен или отклонён, пока шёл запрос
        if code == "FA002":
            raise VersionConflict from exc
        if code == "FA003":
            raise CoupleNotActive from exc
        if code == "FA004":
            raise BadReplyTarget from exc
        if code == "42501":
            raise OwnerGone from exc
        raise
    return SentMessage(
        id=_uuid(m.id),
        text=body,
        is_adapted=bool(m.is_adapted),
        harsh_tone_warning=bool(m.harsh_tone_warning),
        status=m.status,
        reply_to_id=None if m.reply_to_id is None else _uuid(m.reply_to_id),
        sent_at=m.sent_at,
        read_at=m.read_at,
        unavailable=reason,
    )

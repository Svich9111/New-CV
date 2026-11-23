"""Слой доступа к парам и приглашениям (ФТ-03, ФТ-04). Все запросы — внутри user_scope (RLS).

Пары, участники и приглашения меняются только функциями БД (`app_create_couple`,
`app_issue_invite`, `app_accept_invite`, `app_leave_couple`; АР-12, АР-13): прямых
INSERT/UPDATE этих таблиц здесь нет (BACKLOG Б-13). Чтение участников — `app_couple_members`.
В БД уходит только SHA-256 токена, сам токен сюда не передаётся."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import sqlstate, user_scope
from app.schemas import Couple, CoupleMember


class CoupleError(Exception):
    """Отказ функции БД; `sqlstate` — код из АР-13 (FB001…FB008, 42501, 22023).
    23505 индекса одной активной пары приходит как FB005."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


# Коды, которые функции пары возвращают по замыслу (АР-13). Остальные ошибки БД — не
# бизнес-отказ и пробрасываются как есть.
KNOWN_SQLSTATES = frozenset(
    {"FB001", "FB002", "FB003", "FB004", "FB005", "FB006", "FB007", "FB008", "42501", "22023"}
)
# app_create_couple при уже активном членстве отвечает 23505 этого индекса (АР-13). Другие
# нарушения уникальности (например, коллизия хеша токена) — не «уже в паре» и пробрасываются.
ONE_ACTIVE_COUPLE_INDEX = "uq_couple_members_one_active_per_user"


def _constraint(exc: DBAPIError) -> str | None:
    """Имя нарушенного ограничения: у asyncpg — в исходном исключении драйвера."""
    for err in (exc.orig, getattr(exc.orig, "__cause__", None)):
        name = getattr(err, "constraint_name", None)
        if name:
            return str(name)
    return None


@contextmanager
def _db_errors() -> Iterator[None]:
    """Отказы функций пары (АР-13) → CoupleError; прочие ошибки БД — как есть."""
    try:
        yield
    except DBAPIError as exc:
        code = sqlstate(exc)
        if code in KNOWN_SQLSTATES:
            raise CoupleError(code) from exc
        if code == "23505" and _constraint(exc) == ONE_ACTIVE_COUPLE_INDEX:
            raise CoupleError("FB005") from exc
        raise


@dataclass(frozen=True)
class IssuedInvite:
    couple_id: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True)
class InviteInfo:
    inviter_name: str | None
    expires_at: datetime
    status: str
    expired: bool


async def _current_couple(s: AsyncSession, user_id: uuid.UUID) -> Couple | None:
    row = (
        await s.execute(
            text(
                "SELECT c.id, c.status::text AS status FROM couple_members cm "
                "JOIN couples c ON c.id = cm.couple_id WHERE cm.user_id = :uid AND cm.active"
            ),
            {"uid": user_id},
        )
    ).one_or_none()
    if row is None:
        return None
    members = await s.execute(
        text(
            "SELECT user_id, role::text AS role, name FROM app_couple_members(:c) "
            "WHERE active ORDER BY role"
        ),
        {"c": row.id},
    )
    return Couple(
        id=row.id,
        status=row.status,
        members=[
            CoupleMember(role=m.role, name=m.name, is_me=m.user_id == user_id) for m in members
        ],
    )


async def _active_couple_id(s: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
    row = await s.execute(
        text("SELECT couple_id FROM couple_members WHERE user_id = :uid AND active"),
        {"uid": user_id},
    )
    value = row.scalar_one()
    return uuid.UUID(str(value))


async def get_current(sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> Couple | None:
    """Пара, в которой пользователь — активный участник (pending или active); иначе None."""
    async with user_scope(sm, user_id) as s:
        return await _current_couple(s, user_id)


async def create_with_invite(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID, token_hash: bytes
) -> IssuedInvite:
    """Пара в статусе pending и первая ссылка — в одной транзакции: пары без ссылки не бывает."""
    with _db_errors():
        async with user_scope(sm, user_id) as s:
            couple_id = (await s.execute(text("SELECT app_create_couple()"))).scalar_one()
            expires = (
                await s.execute(text("SELECT app_issue_invite(:h)"), {"h": token_hash})
            ).scalar_one()
    return IssuedInvite(uuid.UUID(str(couple_id)), expires)


async def issue_invite(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID, token_hash: bytes
) -> IssuedInvite:
    """Новая ссылка для ожидающей пары; прежние активные отзывает функция."""
    with _db_errors():
        async with user_scope(sm, user_id) as s:
            expires = (
                await s.execute(text("SELECT app_issue_invite(:h)"), {"h": token_hash})
            ).scalar_one()
            couple_id = await _active_couple_id(s, user_id)
    return IssuedInvite(couple_id, expires)


async def find_invite(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID, token_hash: bytes
) -> InviteInfo | None:
    """Приглашение по хешу токена (для входа до онбординга). Срок сравнивается часами БД."""
    async with user_scope(sm, user_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT inviter_name, expires_at, status::text AS status, "
                    "  (status = 'expired' OR expires_at <= now()) AS expired "
                    "FROM app_find_invite(:h)"
                ),
                {"h": token_hash},
            )
        ).one_or_none()
    if row is None:
        return None
    return InviteInfo(row.inviter_name, row.expires_at, row.status, bool(row.expired))


async def accept_invite(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID, token_hash: bytes
) -> Couple:
    with _db_errors():
        async with user_scope(sm, user_id) as s:
            await s.execute(text("SELECT app_accept_invite(:h)"), {"h": token_hash})
            couple = await _current_couple(s, user_id)
    if couple is None:  # функция только что сделала пользователя участником
        raise RuntimeError("accepted couple is not visible")
    return couple


async def leave(sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> None:
    """Выход распускает пару для обоих (АР-13); агенты и память не затрагиваются."""
    with _db_errors():
        async with user_scope(sm, user_id) as s:
            await s.execute(text("SELECT app_leave_couple()"))

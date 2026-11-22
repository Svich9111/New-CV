"""3.3 (A1): лента текущей пары страницами — `app_couple_feed_page` (миграция 0012).

ФТ-10, ФТ-11; ТЗ §4.4, §4.5 п.2, §6.2 п.7; DECISIONS.md АР-11, АР-25, АР-40. Данные — выдуманные,
заводит суперпользователь; лента читается под ролью входа приложения (`user_scope`)."""

import itertools
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import user_scope
from tests.conftest import alembic
from tests.test_couple_functions import SM, _sqlstate

_tg = itertools.count(9_450_000)
PAGE = "SELECT * FROM app_couple_feed_page(:n, :ba, :bi)"


async def _user(engine: AsyncEngine) -> uuid.UUID:
    uid = uuid.uuid4()
    async with engine.begin() as c:
        await c.execute(
            text("INSERT INTO users (id, telegram_id) VALUES (:u, :tg)"),
            {"u": uid, "tg": next(_tg)},
        )
    return uid


async def _couple(engine: AsyncEngine, a: uuid.UUID, b: uuid.UUID, *, active: bool) -> uuid.UUID:
    cid = uuid.uuid4()
    async with engine.begin() as c:
        await c.execute(
            text("INSERT INTO couples (id, status) VALUES (:c, CAST(:s AS couple_status))"),
            {"c": cid, "s": "active" if active else "dissolved"},
        )
        for who, role in ((a, "initiator"), (b, "partner")):
            await c.execute(
                text(
                    "INSERT INTO couple_members (couple_id, user_id, role, active) "
                    "VALUES (:c, :u, CAST(:r AS member_role), :ac)"
                ),
                {"c": cid, "u": who, "r": role, "ac": active},
            )
    return cid


async def _msg(
    engine: AsyncEngine,
    couple: uuid.UUID,
    author: uuid.UUID,
    recipient: uuid.UUID,
    status: str = "delivered",
    sent: str = "2026-11-01 12:00:00+00",
) -> uuid.UUID:
    mid = uuid.uuid4()
    async with engine.begin() as c:
        await c.execute(
            text(
                "INSERT INTO partner_messages (id, couple_id, author_id, recipient_id, "
                "original_enc, adapted_enc, status, sent_at) VALUES (:i, :c, :a, :r, 'orig', "
                "'adapted', CAST(:s AS partner_message_status), CAST(CAST(:t AS text) AS timestamptz))"
            ),
            {"i": mid, "c": couple, "a": author, "r": recipient, "s": status, "t": sent},
        )
    return mid


async def _page(sm: SM, uid: uuid.UUID | None, n: int, before: Any = None) -> list[Any]:
    async with user_scope(sm, uid) as s:
        ba, bi = (before.sent_at, before.id) if before is not None else (None, None)
        return list(await s.execute(text(PAGE), {"n": n, "ba": ba, "bi": bi}))


async def test_only_current_couple_and_visible_statuses(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Только текущая активная пара; автору — оригинал любого статуса, получателю — версия для
    получателя доставленного; бывшая пара и недоставленное получателю не видны."""
    anna, igor, oleg = await _user(engine), await _user(engine), await _user(engine)
    old = await _couple(engine, anna, oleg, active=False)
    cur = await _couple(engine, anna, igor, active=True)
    await _msg(engine, old, oleg, anna)
    mine = await _msg(engine, cur, anna, igor, "processing", "2026-11-02 10:00:00+00")
    seen = await _msg(engine, cur, igor, anna, "read", "2026-11-02 11:00:00+00")
    await _msg(engine, cur, igor, anna, "processing", "2026-11-02 12:00:00+00")
    await _msg(engine, cur, igor, anna, "withheld", "2026-11-02 13:00:00+00")
    rows = await _page(sessionmaker, anna, 100)
    assert [(r.id, r.outgoing, bytes(r.text_enc)) for r in rows] == [
        (seen, False, b"adapted"),
        (mine, True, b"orig"),
    ]
    assert {r.couple_id for r in rows} == {cur}
    igor_rows = await _page(sessionmaker, igor, 100)
    assert len(igor_rows) == 3  # свои — все статусы; входящее от Анны в processing — не видно
    assert mine not in {r.id for r in igor_rows}
    async with engine.begin() as c:
        await c.execute(text("UPDATE couples SET status = 'dissolved' WHERE id = :c"), {"c": cur})
    assert await _page(sessionmaker, anna, 100) == []  # обмен прекращается (§4.5 п.2)


async def test_keyset_pages_without_loss_on_equal_time(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Порядок (sent_at, id) и курсор по ключу: 7 сообщений с одним временем и 3 позже по 2 на
    страницу — все ровно по разу, от новых к старым."""
    anna, igor = await _user(engine), await _user(engine)
    cur = await _couple(engine, anna, igor, active=True)
    same = [await _msg(engine, cur, igor, anna) for _ in range(7)]
    later = [
        await _msg(engine, cur, anna, igor, "delivered", f"2026-11-03 0{i}:00:00+00")
        for i in range(3)
    ]
    got: list[Any] = []
    before = None
    for _ in range(10):
        page = await _page(sessionmaker, anna, 2, before)
        if not page:
            break
        got += page
        before = page[-1]
    ids = [r.id for r in got]
    assert ids == list(reversed(later)) + sorted(same, reverse=True)
    keys = [(r.sent_at, r.id) for r in got]
    assert keys == sorted(keys, reverse=True)


async def test_arguments_and_errors(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Курсор целиком или никак (22023); без пары — пусто; без входа и неизвестный — 42501;
    предел 1–100."""
    anna, igor = await _user(engine), await _user(engine)
    loner = await _user(engine)
    cur = await _couple(engine, anna, igor, active=True)
    for _ in range(3):
        await _msg(engine, cur, igor, anna)
    assert await _page(sessionmaker, loner, 10) == []
    assert len(await _page(sessionmaker, anna, 0)) == 1
    assert len(await _page(sessionmaker, anna, 1000)) == 3
    for ba, bi in (("2026-11-01 12:00:00+00", None), (None, uuid.uuid4())):
        with pytest.raises(DBAPIError) as ei:
            async with user_scope(sessionmaker, anna) as s:
                await s.execute(
                    text(
                        "SELECT * FROM app_couple_feed_page(5, CAST(CAST(:ba AS text) AS timestamptz), :bi)"
                    ),
                    {"ba": ba, "bi": bi},
                )
        assert _sqlstate(ei.value) == "22023"
    for uid in (None, uuid.uuid4()):
        with pytest.raises(DBAPIError) as ei:
            await _page(sessionmaker, uid, 5)
        assert _sqlstate(ei.value) == "42501"


async def test_privileges(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        for role in ("feelings_app", "feelings_api", "feelings_worker", "feelings_admin", "public"):
            row = await conn.execute(
                text(
                    "SELECT has_function_privilege(:r, "
                    "'app_couple_feed_page(integer, timestamp with time zone, uuid)', 'EXECUTE')"
                ),
                {"r": role},
            )
            assert row.scalar_one() is (role == "feelings_app"), role


async def test_downgrade_0011_upgrade_check(engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    """Downgrade до 0011 удаляет функцию и индекс; upgrade и `alembic check` чисты."""
    alembic("downgrade", "0011")
    await engine.dispose()
    await app_engine.dispose()
    try:
        async with engine.connect() as conn:
            n = await conn.execute(
                text("SELECT count(*) FROM pg_proc WHERE proname = 'app_couple_feed_page'")
            )
            assert n.scalar_one() == 0
            idx = await conn.execute(
                text(
                    "SELECT count(*) FROM pg_indexes "
                    "WHERE indexname = 'ix_partner_messages_couple_sent'"
                )
            )
            assert idx.scalar_one() == 0
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    alembic("check")

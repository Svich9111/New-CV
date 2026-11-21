"""0.7b: выдача и принятие приглашения, выход из пары — функции миграции 0002.

ФТ-03, ФТ-04; ТЗ §4.1 п.3–6, §4.5 п.2, §7; АР-7, АР-12, АР-13. Данные заводит
суперпользователь (`engine`), действия — под ролью приложения (`sessionmaker` + `user_scope`).
Данные выдуманные (CLAUDE.md)."""

import asyncio
import hashlib
import itertools
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db import user_scope

_tg = itertools.count(7_000_000)
SM = async_sessionmaker[AsyncSession]


def _token() -> bytes:
    return hashlib.sha256(uuid.uuid4().bytes).digest()


def _sqlstate(exc: DBAPIError) -> str | None:
    orig = exc.orig
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


async def _user(engine: AsyncEngine, name: str) -> uuid.UUID:
    """Пользователь с агентом и записью памяти (проверяем, что выход их не трогает)."""
    uid, aid = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (id, telegram_id, name) VALUES (:id, :tg, :n)"),
            {"id": uid, "tg": next(_tg), "n": name},
        )
        await conn.execute(
            text("INSERT INTO agents (id, user_id) VALUES (:a, :u)"), {"a": aid, "u": uid}
        )
        await conn.execute(
            text(
                "INSERT INTO memory_items (agent_id, type, text_enc, source) "
                "VALUES (:a, 'fact', :t, 'owner_dialog')"
            ),
            {"a": aid, "t": f"память {name}".encode()},
        )
    return uid


async def _call(sm: SM, uid: uuid.UUID | None, sql: str, **params: object) -> object:
    async with user_scope(sm, uid) as s:
        return (await s.execute(text(sql), params)).scalar_one()


async def _fails(sm: SM, uid: uuid.UUID | None, code: str, sql: str, **params: object) -> None:
    with pytest.raises(DBAPIError) as ei:
        await _call(sm, uid, sql, **params)
    assert _sqlstate(ei.value) == code, ei.value


async def _pending(
    sm: SM, engine: AsyncEngine, name: str = "Вера"
) -> tuple[uuid.UUID, uuid.UUID, bytes]:
    """Инициатор с ожидающей парой и действующей ссылкой: (uid, couple_id) и хеш токена."""
    uid = await _user(engine, name)
    couple = await _call(sm, uid, "SELECT app_create_couple()")
    token = _token()
    await _call(sm, uid, "SELECT app_issue_invite(:h)", h=token)
    return uid, uuid.UUID(str(couple)), token


async def _row(engine: AsyncEngine, sql: str, **params: object) -> dict[str, object]:
    async with engine.connect() as conn:
        return dict((await conn.execute(text(sql), params)).mappings().one())


async def _members(engine: AsyncEngine, couple: uuid.UUID) -> dict[uuid.UUID, dict[str, object]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT user_id, role, active, left_at FROM couple_members WHERE couple_id = :c"),
            {"c": couple},
        )
        return {r.user_id: dict(r._mapping) for r in rows}


ACCEPT = "SELECT app_accept_invite(:h)"
ISSUE = "SELECT app_issue_invite(:h)"
LEAVE = "SELECT app_leave_couple()"


# --- app_issue_invite ------------------------------------------------------------------------


async def test_issue_invite_sets_7_days_and_revokes_previous(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """ТЗ §4.1 п.6: срок 7 дней ставит функция; прежняя ссылка пары отзывается."""
    uid, couple, first = await _pending(sessionmaker, engine)
    second = _token()
    before = datetime.now(UTC)
    expires = await _call(sessionmaker, uid, ISSUE, h=second)
    assert isinstance(expires, datetime)
    assert (
        timedelta(days=7) - timedelta(minutes=1) < expires - before < timedelta(days=7, minutes=1)
    )
    old = await _row(engine, "SELECT status FROM invites WHERE token_hash = :h", h=first)
    new = await _row(
        engine,
        "SELECT status, couple_id, created_by, expires_at FROM invites WHERE token_hash = :h",
        h=second,
    )
    assert old["status"] == "revoked"
    assert new == {
        "status": "active",
        "couple_id": couple,
        "created_by": uid,
        "expires_at": expires,
    }
    await _fails(sessionmaker, await _user(engine, "Глеб"), "FB003", ACCEPT, h=first)


async def test_issue_invite_requires_pending_couple(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Ссылку выдаёт только активный участник ожидающей пары."""
    loner = await _user(engine, "Лев")
    await _fails(sessionmaker, loner, "FB007", ISSUE, h=_token())
    uid, _, token = await _pending(sessionmaker, engine)
    partner = await _user(engine, "Пётр")
    await _call(sessionmaker, partner, ACCEPT, h=token)
    for person in (uid, partner):  # пара уже активна
        await _fails(sessionmaker, person, "FB006", ISSUE, h=_token())


async def test_issue_invite_rejects_bad_input(sessionmaker: SM, engine: AsyncEngine) -> None:
    uid, _, _ = await _pending(sessionmaker, engine)
    await _fails(sessionmaker, uid, "22023", ISSUE, h=b"short")
    await _fails(sessionmaker, None, "42501", ISSUE, h=_token())


# --- app_accept_invite -----------------------------------------------------------------------


async def test_accept_invite_activates_couple(sessionmaker: SM, engine: AsyncEngine) -> None:
    """ФТ-03: приглашение становится used, пара — active, принимающий — partner."""
    initiator, couple, token = await _pending(sessionmaker, engine)
    partner = await _user(engine, "Марк")
    got = await _call(sessionmaker, partner, ACCEPT, h=token)
    assert got == couple
    c = await _row(engine, "SELECT status, activated_at FROM couples WHERE id = :c", c=couple)
    assert c["status"] == "active" and c["activated_at"] is not None
    inv = await _row(
        engine, "SELECT status, used_by, used_at FROM invites WHERE token_hash = :h", h=token
    )
    assert inv["status"] == "used" and inv["used_by"] == partner and inv["used_at"] is not None
    members = await _members(engine, couple)
    assert {k: (v["role"], v["active"]) for k, v in members.items()} == {
        initiator: ("initiator", True),
        partner: ("partner", True),
    }
    # Партнёр видит свою пару через RLS.
    seen = await _call(sessionmaker, partner, "SELECT status FROM couples WHERE id = :c", c=couple)
    assert seen == "active"


async def test_accept_invite_is_single_use(sessionmaker: SM, engine: AsyncEngine) -> None:
    _, _, token = await _pending(sessionmaker, engine)
    partner, other = await _user(engine, "Ян"), await _user(engine, "Ия")
    await _call(sessionmaker, partner, ACCEPT, h=token)
    await _fails(sessionmaker, other, "FB003", ACCEPT, h=token)
    await _fails(sessionmaker, partner, "FB003", ACCEPT, h=token)


async def test_accept_unknown_invite(sessionmaker: SM, engine: AsyncEngine) -> None:
    await _fails(sessionmaker, await _user(engine, "Ада"), "FB001", ACCEPT, h=_token())
    await _fails(sessionmaker, None, "42501", ACCEPT, h=_token())


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE invites SET expires_at = now() - interval '1 second' WHERE token_hash = :h",
        "UPDATE invites SET status = 'expired' WHERE token_hash = :h",
    ],
)
async def test_accept_expired_invite(sessionmaker: SM, engine: AsyncEngine, sql: str) -> None:
    _, couple, token = await _pending(sessionmaker, engine)
    async with engine.begin() as conn:
        await conn.execute(text(sql), {"h": token})
    await _fails(sessionmaker, await _user(engine, "Ада"), "FB002", ACCEPT, h=token)
    status = await _row(engine, "SELECT status FROM couples WHERE id = :c", c=couple)
    assert status["status"] == "pending"


async def test_accept_revoked_invite(sessionmaker: SM, engine: AsyncEngine) -> None:
    uid, _, token = await _pending(sessionmaker, engine)
    async with user_scope(sessionmaker, uid) as s:  # отзыв напрямую, по политике invites_revoke
        await s.execute(
            text("UPDATE invites SET status = 'revoked' WHERE token_hash = :h"), {"h": token}
        )
    await _fails(sessionmaker, await _user(engine, "Ада"), "FB003", ACCEPT, h=token)


async def test_accept_own_invite(sessionmaker: SM, engine: AsyncEngine) -> None:
    uid, couple, token = await _pending(sessionmaker, engine)
    await _fails(sessionmaker, uid, "FB004", ACCEPT, h=token)
    assert len(await _members(engine, couple)) == 1


async def test_accept_when_already_in_couple(sessionmaker: SM, engine: AsyncEngine) -> None:
    """ФТ-04: одна активная пара — и ожидающая, и активная мешают принять приглашение."""
    _, couple, token = await _pending(sessionmaker, engine, "Инна")
    waiting, _, _ = await _pending(sessionmaker, engine, "Ждан")
    _, _, token2 = await _pending(sessionmaker, engine, "Рита")
    paired = await _user(engine, "Олег")
    await _call(sessionmaker, paired, ACCEPT, h=token2)
    for person in (waiting, paired):
        await _fails(sessionmaker, person, "FB005", ACCEPT, h=token)
    inv = await _row(engine, "SELECT status FROM invites WHERE token_hash = :h", h=token)
    assert inv["status"] == "active"
    assert len(await _members(engine, couple)) == 1


@pytest.mark.parametrize(
    "sql",
    [
        # Пара распалась в обход функций, а ссылка осталась активной.
        "UPDATE couples SET status = 'dissolved', dissolved_at = now() WHERE id = :c",
        # Инициатор вышел, пара формально ожидает, активных участников нет.
        "UPDATE couple_members SET active = false, left_at = now() WHERE couple_id = :c",
    ],
)
async def test_accept_when_couple_not_pending(
    sessionmaker: SM, engine: AsyncEngine, sql: str
) -> None:
    _, couple, token = await _pending(sessionmaker, engine)
    async with engine.begin() as conn:
        await conn.execute(text(sql), {"c": couple})
    await _fails(sessionmaker, await _user(engine, "Ада"), "FB006", ACCEPT, h=token)
    inv = await _row(engine, "SELECT status FROM invites WHERE token_hash = :h", h=token)
    assert inv["status"] == "active"


async def test_concurrent_accepts_one_wins(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Гонка: два одновременных принятия одной ссылки — успешно ровно одно.

    Строку приглашения держит третья транзакция, пока обе попытки не встанут в ожидание
    блокировки; так проверка не зависит от удачного совпадения по времени."""
    _, couple, token = await _pending(sessionmaker, engine)
    first, second = await _user(engine, "Ася"), await _user(engine, "Тимур")

    async def attempt(uid: uuid.UUID) -> object:
        try:
            return await _call(sessionmaker, uid, ACCEPT, h=token)
        except DBAPIError as exc:
            return _sqlstate(exc)

    async with engine.connect() as holder:
        await holder.execute(
            text("SELECT 1 FROM invites WHERE token_hash = :h FOR UPDATE"), {"h": token}
        )
        tasks = asyncio.gather(attempt(first), attempt(second))
        async with engine.connect() as watch:
            for _ in range(200):
                waiting = await watch.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND wait_event_type = 'Lock' AND query LIKE '%app_accept_invite%'"
                    )
                )
                if waiting.scalar_one() >= 2:
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("обе попытки не дошли до блокировки")
        await holder.rollback()
        results = await tasks
    assert sorted(map(str, results)) == sorted([str(couple), "FB003"])
    winner = first if results[0] == couple else second
    members = await _members(engine, couple)
    assert len(members) == 2 and members[winner]["role"] == "partner"
    inv = await _row(engine, "SELECT status, used_by FROM invites WHERE token_hash = :h", h=token)
    assert inv == {"status": "used", "used_by": winner}


# --- app_leave_couple ------------------------------------------------------------------------


async def _counts(sm: SM, uid: uuid.UUID) -> tuple[int, int]:
    async with user_scope(sm, uid) as s:
        agents = (await s.execute(text("SELECT count(*) FROM agents"))).scalar_one()
        memory = (await s.execute(text("SELECT count(*) FROM memory_items"))).scalar_one()
        return int(agents), int(memory)


@pytest.mark.parametrize("who", ["initiator", "partner"])
async def test_leave_active_couple(sessionmaker: SM, engine: AsyncEngine, who: str) -> None:
    """ТЗ §4.5 п.2, ФТ-04: любой выходит; пара распадается; агенты и память на месте;
    оба могут создать новую пару; история пары остаётся видна (Д7)."""
    initiator, couple, token = await _pending(sessionmaker, engine)
    partner = await _user(engine, "Ник")
    await _call(sessionmaker, partner, ACCEPT, h=token)
    async with engine.begin() as conn:  # «висящая» ссылка пары должна быть отозвана
        await conn.execute(
            text(
                "INSERT INTO invites (token_hash, couple_id, created_by, expires_at) "
                "VALUES (:h, :c, :u, now() + interval '1 day')"
            ),
            {"h": _token(), "c": couple, "u": initiator},
        )
    leaver = initiator if who == "initiator" else partner
    before = {u: await _counts(sessionmaker, u) for u in (initiator, partner)}
    assert await _call(sessionmaker, leaver, LEAVE) == couple
    c = await _row(engine, "SELECT status, dissolved_at FROM couples WHERE id = :c", c=couple)
    assert c["status"] == "dissolved" and c["dissolved_at"] is not None
    members = await _members(engine, couple)
    assert all(not m["active"] and m["left_at"] is not None for m in members.values())
    active = await _row(
        engine,
        "SELECT count(*) AS n FROM invites WHERE couple_id = :c AND status = 'active'",
        c=couple,
    )
    assert active["n"] == 0
    for u in (initiator, partner):
        assert await _counts(sessionmaker, u) == before[u] == (1, 1)
        seen = await _call(sessionmaker, u, "SELECT status FROM couples WHERE id = :c", c=couple)
        assert seen == "dissolved"
        await _fails(sessionmaker, u, "FB007", LEAVE)
        await _fails(sessionmaker, u, "FB007", ISSUE, h=_token())
        new = await _call(sessionmaker, u, "SELECT app_create_couple()")
        assert new != couple


async def test_leave_pending_couple_revokes_invite(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Б-6: инициатор вышел из ожидающей пары — его ссылка больше не работает."""
    uid, couple, token = await _pending(sessionmaker, engine)
    assert await _call(sessionmaker, uid, LEAVE) == couple
    inv = await _row(engine, "SELECT status FROM invites WHERE token_hash = :h", h=token)
    assert inv["status"] == "revoked"
    c = await _row(engine, "SELECT status FROM couples WHERE id = :c", c=couple)
    assert c["status"] == "dissolved"
    await _fails(sessionmaker, await _user(engine, "Ада"), "FB003", ACCEPT, h=token)


async def test_leave_without_couple(sessionmaker: SM, engine: AsyncEngine) -> None:
    await _fails(sessionmaker, await _user(engine, "Ада"), "FB007", LEAVE)
    await _fails(sessionmaker, None, "42501", LEAVE)


async def test_leave_does_not_touch_other_couples(sessionmaker: SM, engine: AsyncEngine) -> None:
    uid, _, _ = await _pending(sessionmaker, engine, "Эля")
    _, other_couple, other_token = await _pending(sessionmaker, engine, "Фома")
    await _call(sessionmaker, uid, LEAVE)
    c = await _row(engine, "SELECT status FROM couples WHERE id = :c", c=other_couple)
    inv = await _row(engine, "SELECT status FROM invites WHERE token_hash = :h", h=other_token)
    assert c["status"] == "pending" and inv["status"] == "active"


# --- Права ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "f", ["app_issue_invite(bytea)", "app_accept_invite(bytea)", "app_leave_couple()"]
)
async def test_execute_only_for_app_role(engine: AsyncEngine, f: str) -> None:
    async with engine.connect() as conn:
        for role, allowed in (
            ("feelings_app", True),
            ("feelings_api", False),
            ("feelings_worker", False),
            ("feelings_admin", False),
            ("public", False),
        ):
            row = await conn.execute(
                text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"), {"r": role, "f": f}
            )
            assert row.scalar_one() is allowed, (role, f)

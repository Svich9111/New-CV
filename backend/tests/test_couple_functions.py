"""0.7b: выдача и принятие приглашения, выход из пары — функции миграций 0002–0004.

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
from tests.conftest import alembic

_tg = itertools.count(7_000_000)
SM = async_sessionmaker[AsyncSession]


def _token() -> bytes:
    return hashlib.sha256(uuid.uuid4().bytes).digest()


def _sqlstate(exc: DBAPIError) -> str | None:
    orig = exc.orig
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


ONBOARDED = (
    "consent_terms_at = now(), consent_data_at = now(), consent_sensitive_at = now(), "
    "age_confirmed_at = now()"
)


async def _user(engine: AsyncEngine, name: str, *, onboarded: bool = True) -> uuid.UUID:
    """Пользователь с агентом и записью памяти (проверяем, что выход их не трогает).
    По умолчанию прошёл онбординг: согласия и 18+ (ТЗ §4.1 п.2)."""
    uid, aid = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (id, telegram_id, name) VALUES (:id, :tg, :n)"),
            {"id": uid, "tg": next(_tg), "n": name},
        )
        if onboarded:
            await conn.execute(text(f"UPDATE users SET {ONBOARDED} WHERE id = :u"), {"u": uid})
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


async def _wait_lock_waiters(engine: AsyncEngine, func: str, n: int) -> None:
    """Ждёт, пока n сеансов с вызовом func встанут в ожидание блокировки.

    AUTOCOMMIT: каждый опрос — своя транзакция, снимок pg_stat_activity свежий (ревью M1)."""
    async with engine.connect() as conn:
        watch = await conn.execution_options(isolation_level="AUTOCOMMIT")
        for _ in range(400):
            waiting = await watch.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND query LIKE :q"
                ),
                {"q": f"%{func}(%"},
            )
            if waiting.scalar_one() >= n:
                return
            await asyncio.sleep(0.025)
    pytest.fail(f"{n} вызова {func} не дошли до блокировки")


async def _try(sm: SM, uid: uuid.UUID, sql: str, **params: object) -> object:
    """Результат вызова или SQLSTATE ошибки (для гонок)."""
    try:
        return await _call(sm, uid, sql, **params)
    except DBAPIError as exc:
        return _sqlstate(exc)


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
    _, _, token = await _pending(sessionmaker, engine)
    async with engine.begin() as conn:  # отзыв в обход функций (у приложения прав нет, Б-13)
        await conn.execute(
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

    async with engine.connect() as holder:
        await holder.execute(
            text("SELECT 1 FROM invites WHERE token_hash = :h FOR UPDATE"), {"h": token}
        )
        tasks = asyncio.gather(
            _try(sessionmaker, first, ACCEPT, h=token), _try(sessionmaker, second, ACCEPT, h=token)
        )
        try:
            await _wait_lock_waiters(engine, "app_accept_invite", 2)
        finally:
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


# --- Раунд 1 ревью 0.7b: миграция 0003 ------------------------------------------------------


@pytest.mark.parametrize("h", [None, b"", b"short", b"x" * 33])
async def test_accept_rejects_bad_hash(
    sessionmaker: SM, engine: AsyncEngine, h: bytes | None
) -> None:
    """n5 / A8 D-2: хеш токена проверяется так же, как в app_issue_invite."""
    await _fails(sessionmaker, await _user(engine, "Ада"), "22023", ACCEPT, h=h)


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        ("SELECT app_create_couple()", {}),
        (ISSUE, {"h": b"i" * 32}),
        (ACCEPT, {"h": b"a" * 32}),
        (LEAVE, {}),
    ],
)
async def test_unknown_or_deleted_user_rejected(
    sessionmaker: SM, engine: AsyncEngine, sql: str, params: dict[str, object]
) -> None:
    """A8 D-3: несуществующий или удалённый app.user_id — 42501, а не ошибка FK."""
    deleted = await _user(engine, "Удалённый")
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": deleted}
        )
    for uid in (uuid.uuid4(), deleted):
        await _fails(sessionmaker, uid, "42501", sql, **params)


@pytest.mark.parametrize(
    "missing", ["consent_terms_at", "consent_data_at", "consent_sensitive_at", "age_confirmed_at"]
)
async def test_onboarding_required_to_create_or_join(
    sessionmaker: SM, engine: AsyncEngine, missing: str
) -> None:
    """n1: ТЗ §4.1 п.2, п.4, §9.3 — пару создаёт и принимает приглашение только прошедший
    онбординг (все согласия и 18+); иначе FB008, ничего не меняется."""
    _, couple, token = await _pending(sessionmaker, engine)
    newbie = await _user(engine, "Новичок")
    async with engine.begin() as conn:
        await conn.execute(text(f"UPDATE users SET {missing} = NULL WHERE id = :u"), {"u": newbie})
    await _fails(sessionmaker, newbie, "FB008", ACCEPT, h=token)
    await _fails(sessionmaker, newbie, "FB008", "SELECT app_create_couple()")
    inv = await _row(engine, "SELECT status FROM invites WHERE token_hash = :h", h=token)
    assert inv["status"] == "active"
    assert len(await _members(engine, couple)) == 1
    n = await _row(engine, "SELECT count(*) AS n FROM couple_members WHERE user_id = :u", u=newbie)
    assert n["n"] == 0


async def test_leave_allowed_without_onboarding(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Выйти из пары можно и после отзыва подтверждения 18+ (выход не требует онбординга)."""
    uid, couple, _ = await _pending(sessionmaker, engine)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET age_confirmed_at = NULL WHERE id = :u"), {"u": uid}
        )
    assert await _call(sessionmaker, uid, LEAVE) == couple


async def test_leave_writes_audit_without_texts(sessionmaker: SM, engine: AsyncEngine) -> None:
    """n2: в audit_log видно, кто вышел (ТЗ §7: кто, действие, объект, время), без текстов."""
    _, couple, token = await _pending(sessionmaker, engine)
    partner = await _user(engine, "Ник")
    await _call(sessionmaker, partner, ACCEPT, h=token)
    await _call(sessionmaker, partner, LEAVE)
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT actor_id, action, object_type, object_id FROM audit_log "
                    "WHERE object_id = :c"
                ),
                {"c": couple},
            )
        ).all()
        cols = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'audit_log'"
            )
        )
        assert {c[0] for c in cols} == {
            "id",
            "actor_id",
            "action",
            "object_type",
            "object_id",
            "created_at",
        }
    assert [tuple(r) for r in rows] == [(partner, "couple.leave", "couple", couple)]


async def test_concurrent_leaves_second_gets_fb007(sessionmaker: SM, engine: AsyncEngine) -> None:
    """n3: оба партнёра выходят одновременно — успешен один, второй получает FB007;
    dissolved_at не перезаписывается, в журнале одна запись."""
    initiator, couple, token = await _pending(sessionmaker, engine)
    partner = await _user(engine, "Ник")
    await _call(sessionmaker, partner, ACCEPT, h=token)
    async with engine.connect() as holder:
        await holder.execute(text("SELECT 1 FROM couples WHERE id = :c FOR UPDATE"), {"c": couple})
        tasks = asyncio.gather(
            _try(sessionmaker, initiator, LEAVE), _try(sessionmaker, partner, LEAVE)
        )
        try:
            await _wait_lock_waiters(engine, "app_leave_couple", 2)
        finally:
            await holder.rollback()
        results = await tasks
    assert sorted(map(str, results)) == sorted([str(couple), "FB007"])
    c = await _row(engine, "SELECT dissolved_at, now() AS now FROM couples WHERE id = :c", c=couple)
    audit = await _row(
        engine,
        "SELECT count(*) AS n, max(created_at) AS at FROM audit_log "
        "WHERE object_id = :c AND action = 'couple.leave'",
        c=couple,
    )
    assert audit["n"] == 1
    assert c["dissolved_at"] == audit["at"]  # одна транзакция выхода — одно now()


async def test_concurrent_create_and_accept_same_user(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """n7: app_create_couple блокирует строку пользователя, как и принятие приглашения;
    одновременное принятие тем же пользователем получает FB005, а не 23505."""
    _, couple, token = await _pending(sessionmaker, engine)
    user = await _user(engine, "Двойной")
    async with engine.connect() as holder:
        await holder.execute(text("SELECT 1 FROM users WHERE id = :u FOR UPDATE"), {"u": user})
        create = asyncio.ensure_future(_try(sessionmaker, user, "SELECT app_create_couple()"))
        try:
            await _wait_lock_waiters(engine, "app_create_couple", 1)
            accept = asyncio.ensure_future(_try(sessionmaker, user, ACCEPT, h=token))
            await _wait_lock_waiters(engine, "app_accept_invite", 1)
        finally:
            await holder.rollback()
        created, accepted = await create, await accept
    assert isinstance(created, uuid.UUID) and created != couple
    assert accepted == "FB005"
    assert len(await _members(engine, couple)) == 1


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


# --- Задача 1.3: миграция 0004 ---------------------------------------------------------------

# Б-13: прямая запись в обход функций. Каждый запрос подставляет свои параметры из словаря.
DIRECT_WRITES = {
    "member_leave": "UPDATE couple_members SET active = false, left_at = now() WHERE user_id = :u",
    "member_rejoin": "UPDATE couple_members SET active = true WHERE user_id = :u",
    "dissolve": "UPDATE couples SET status = 'dissolved', dissolved_at = now() WHERE id = :c",
    "invite_revoke": "UPDATE invites SET status = 'revoked' WHERE token_hash = :h",
    "invite_insert": (
        "INSERT INTO invites (token_hash, couple_id, created_by, expires_at) "
        "VALUES (:n, :c, :u, now() + interval '1 day')"
    ),
}


@pytest.mark.parametrize("name", sorted(DIRECT_WRITES))
async def test_direct_couple_writes_denied(
    sessionmaker: SM, engine: AsyncEngine, name: str
) -> None:
    """Б-13 (АР-13): пара, участники и приглашения меняются только функциями — прямой
    UPDATE/INSERT отклоняется правами (permission denied), состояние не меняется."""
    uid, couple, token = await _pending(sessionmaker, engine)
    params = {"u": uid, "c": couple, "h": token, "n": _token()}
    with pytest.raises(DBAPIError) as ei:
        await _call(sessionmaker, uid, DIRECT_WRITES[name] + " RETURNING 1", **params)
    assert _sqlstate(ei.value) == "42501", ei.value
    assert "permission denied" in str(ei.value)
    c = await _row(engine, "SELECT status, dissolved_at FROM couples WHERE id = :c", c=couple)
    assert c == {"status": "pending", "dissolved_at": None}
    members = await _members(engine, couple)
    assert [(m["active"], m["left_at"]) for m in members.values()] == [(True, None)]
    inv = await _row(
        engine,
        "SELECT count(*) FILTER (WHERE status = 'active') AS active, count(*) AS n "
        "FROM invites WHERE couple_id = :c",
        c=couple,
    )
    assert inv == {"active": 1, "n": 1}


async def test_couple_tables_read_only_for_app(engine: AsyncEngine) -> None:
    """Б-13: у feelings_app на couples, couple_members, invites — только SELECT (ни на одну
    колонку нет INSERT/UPDATE, нет DELETE); политики записи из 0001 удалены, чтения — на месте."""
    async with engine.connect() as conn:
        for table in ("couples", "couple_members", "invites"):
            row = await conn.execute(
                text(
                    "SELECT has_table_privilege('feelings_app', :t, 'SELECT'), "
                    "has_table_privilege('feelings_app', :t, 'DELETE'), "
                    "has_any_column_privilege('feelings_app', :t, 'INSERT'), "
                    "has_any_column_privilege('feelings_app', :t, 'UPDATE')"
                ),
                {"t": table},
            )
            assert tuple(row.one()) == (True, False, False, False), table
        policies = await conn.execute(
            text(
                "SELECT policyname, cmd FROM pg_policies "
                "WHERE tablename IN ('couples', 'couple_members', 'invites')"
            )
        )
        assert sorted(map(tuple, policies)) == [
            ("couples_select", "SELECT"),
            ("invites_select", "SELECT"),
            ("members_select", "SELECT"),
        ]


@pytest.mark.parametrize(
    "missing", ["consent_terms_at", "consent_data_at", "consent_sensitive_at", "age_confirmed_at"]
)
async def test_issue_invite_requires_onboarding_fb008(
    sessionmaker: SM, engine: AsyncEngine, missing: str
) -> None:
    """D-4 (0.7b-test): ТЗ §4.1 п.2, §9.3 — без любого из согласий или 18+ участник ожидающей
    пары новую ссылку не получает (FB008); действующая ссылка не отзывается, новой не
    появляется. Выйти из пары по-прежнему можно; после онбординга ссылка выдаётся."""
    uid, couple, token = await _pending(sessionmaker, engine)
    async with engine.begin() as conn:
        await conn.execute(text(f"UPDATE users SET {missing} = NULL WHERE id = :u"), {"u": uid})
    await _fails(sessionmaker, uid, "FB008", ISSUE, h=_token())
    inv = await _row(
        engine,
        "SELECT count(*) AS n, bool_and(token_hash = :h AND status = 'active') AS same "
        "FROM invites WHERE couple_id = :c",
        c=couple,
        h=token,
    )
    assert inv == {"n": 1, "same": True}
    async with engine.begin() as conn:
        await conn.execute(text(f"UPDATE users SET {missing} = now() WHERE id = :u"), {"u": uid})
    assert isinstance(await _call(sessionmaker, uid, ISSUE, h=_token()), datetime)
    async with engine.begin() as conn:
        await conn.execute(text(f"UPDATE users SET {missing} = NULL WHERE id = :u"), {"u": uid})
    assert await _call(sessionmaker, uid, LEAVE) == couple


async def test_issue_invite_check_order(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Порядок проверок app_issue_invite (АР-13): 22023 → 42501 → FB008 → FB007."""
    newbie = await _user(engine, "Новичок", onboarded=False)  # не в паре и без онбординга
    await _fails(sessionmaker, newbie, "22023", ISSUE, h=b"short")
    await _fails(sessionmaker, uuid.uuid4(), "22023", ISSUE, h=None)
    await _fails(sessionmaker, uuid.uuid4(), "42501", ISSUE, h=_token())
    await _fails(sessionmaker, newbie, "FB008", ISSUE, h=_token())
    loner = await _user(engine, "Одиночка")
    await _fails(sessionmaker, loner, "FB007", ISSUE, h=_token())


async def test_migration_0004_reversible(engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    """0004 → 0003 → 0004: downgrade возвращает права и политики записи 0001 и
    app_issue_invite без FB008; повторный upgrade снова их убирает."""

    async def state() -> tuple[bool, int, bool]:
        async with engine.connect() as conn:
            row = await conn.execute(
                text(
                    "SELECT has_any_column_privilege('feelings_app', 'couples', 'UPDATE'), "
                    "(SELECT count(*) FROM pg_policies "
                    " WHERE tablename IN ('couples', 'couple_members', 'invites')), "
                    "position('FB008' IN "
                    "pg_get_functiondef('app_issue_invite(bytea)'::regprocedure)) > 0"
                )
            )
            update, policies, fb008 = row.one()
            return bool(update), int(policies), bool(fb008)

    try:
        assert await state() == (False, 3, True)
        alembic("downgrade", "0003")
        await engine.dispose()
        assert await state() == (True, 7, False)
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    assert await state() == (False, 3, True)

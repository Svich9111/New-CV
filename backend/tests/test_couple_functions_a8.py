"""A8, задача 0.7b: независимые тесты функций пары (миграция 0002).

ФТ-03 (приглашение по ссылке), ФТ-04 (одна активная пара, выход, новая пара);
ТЗ §4.1 п.3–6, §4.5 п.2, §7; DECISIONS.md АР-7, АР-12, АР-13, Д7; BACKLOG Б-13.
Данные заводит суперпользователь (`engine`), действия — под ролью приложения
(`sessionmaker` + `user_scope`, вход feelings_api → SET ROLE feelings_app).
Данные выдуманные (CLAUDE.md). Роли сервера БД не меняются: SET ROLE — только внутри
откатываемых транзакций."""

import asyncio
import hashlib
import uuid
from collections.abc import Awaitable
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from app.db import user_scope

SM = async_sessionmaker[AsyncSession]
ACCEPT = "SELECT app_accept_invite(:h)"
ISSUE = "SELECT app_issue_invite(:h)"
LEAVE = "SELECT app_leave_couple()"
CREATE = "SELECT app_create_couple()"
RACE_TIMEOUT = 15.0


def _token() -> bytes:
    return hashlib.sha256(uuid.uuid4().bytes).digest()


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


# Онбординг пройден: все согласия и подтверждение 18+ (ТЗ §4.1 п.2; FB008, миграция 0003).
ONBOARDING_COLUMNS = (
    "consent_terms_at",
    "consent_data_at",
    "consent_sensitive_at",
    "age_confirmed_at",
)


async def _user(engine: AsyncEngine, name: str, *, onboarded: bool = True) -> uuid.UUID:
    """Пользователь с агентом и одной записью памяти; по умолчанию прошёл онбординг."""
    uid = uuid.uuid4()
    stamps = ", ".join(f"{c} = now()" for c in ONBOARDING_COLUMNS)
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (id, telegram_id, name) VALUES (:id, :tg, :n)"),
            {"id": uid, "tg": 9_000_000_000 + uuid.uuid4().int % 10**9, "n": name},
        )
        if onboarded:
            await conn.execute(text(f"UPDATE users SET {stamps} WHERE id = :u"), {"u": uid})
        aid = (
            await conn.execute(
                text("INSERT INTO agents (user_id) VALUES (:u) RETURNING id"), {"u": uid}
            )
        ).scalar_one()
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


async def _try(sm: SM, uid: uuid.UUID | None, sql: str, **params: object) -> object:
    """Результат вызова или SQLSTATE ошибки (для гонок)."""
    try:
        return await _call(sm, uid, sql, **params)
    except DBAPIError as exc:
        return _sqlstate(exc)


async def _fails(sm: SM, uid: uuid.UUID | None, code: str, sql: str, **params: object) -> None:
    with pytest.raises(DBAPIError) as ei:
        await _call(sm, uid, sql, **params)
    assert _sqlstate(ei.value) == code, ei.value


async def _one(engine: AsyncEngine, sql: str, **params: object) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def _state(engine: AsyncEngine, couple: object) -> dict[str, object]:
    """Сводка пары: статус, активные участники, активные приглашения."""
    async with engine.connect() as conn:
        status = (
            await conn.execute(text("SELECT status FROM couples WHERE id = :c"), {"c": couple})
        ).scalar_one()
        active = (
            await conn.execute(
                text("SELECT count(*) FROM couple_members WHERE couple_id = :c AND active"),
                {"c": couple},
            )
        ).scalar_one()
        invites = (
            await conn.execute(
                text("SELECT count(*) FROM invites WHERE couple_id = :c AND status = 'active'"),
                {"c": couple},
            )
        ).scalar_one()
    return {"status": status, "active_members": active, "active_invites": invites}


async def _pending(sm: SM, engine: AsyncEngine, name: str) -> tuple[uuid.UUID, object, bytes]:
    uid = await _user(engine, name)
    couple = await _call(sm, uid, CREATE)
    token = _token()
    await _call(sm, uid, ISSUE, h=token)
    return uid, couple, token


async def _as_app(conn: AsyncConnection, uid: uuid.UUID | None, role: str = "feelings_app") -> None:
    """Внутри транзакции суперпользователя: SET LOCAL ROLE и app.user_id (откатывается)."""
    await conn.execute(text(f"SET LOCAL ROLE {role}"))
    await conn.execute(
        text("SELECT set_config('app.user_id', :u, true)"), {"u": str(uid) if uid else ""}
    )


async def _wait_locked(engine: AsyncEngine, n: int) -> None:
    """Ждать, пока n сеансов приложения (feelings_api) встанут в ожидание блокировки."""
    async with engine.connect() as watch:
        for _ in range(300):
            got = (
                await watch.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND usename = 'feelings_api' "
                        "AND wait_event_type = 'Lock'"
                    )
                )
            ).scalar_one()
            await watch.rollback()
            if got >= n:
                return
            await asyncio.sleep(0.05)
    pytest.fail(f"не дождались {n} ожидающих блокировку сеансов")


async def _race(
    engine: AsyncEngine, lock_sql: str, params: dict[str, object], *calls: Awaitable[object]
) -> list[object]:
    """Третья транзакция держит строку; вызовы встают в очередь строго по порядку;
    затем блокировка снимается. Взаимоблокировка (40P01) или зависание — провал."""
    async with engine.connect() as holder:
        await holder.execute(text(lock_sql), params)
        tasks = []
        for i, c in enumerate(calls, start=1):
            tasks.append(asyncio.ensure_future(c))
            await _wait_locked(engine, i)
        await holder.rollback()
        results = await asyncio.wait_for(asyncio.gather(*tasks), RACE_TIMEOUT)
    assert "40P01" not in results, results
    return list(results)


# --- Полный жизненный цикл (ФТ-03, ФТ-04, ТЗ §4.1 п.3–6, §4.5 п.2) --------------------------


async def test_full_lifecycle(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Создать → ссылка → принять → выйти → оба создают новые пары → снова вместе."""
    anna, couple, token = await _pending(sessionmaker, engine, "Анна-8")
    igor = await _user(engine, "Игорь-8")
    assert await _state(engine, couple) == {
        "status": "pending",
        "active_members": 1,
        "active_invites": 1,
    }
    assert await _call(sessionmaker, igor, ACCEPT, h=token) == couple
    assert await _state(engine, couple) == {
        "status": "active",
        "active_members": 2,
        "active_invites": 0,
    }
    # ФТ-04: участник активной пары не создаёт вторую.
    for u in (anna, igor):
        await _fails(sessionmaker, u, "23505", CREATE)
    assert await _call(sessionmaker, igor, LEAVE) == couple
    assert await _state(engine, couple) == {
        "status": "dissolved",
        "active_members": 0,
        "active_invites": 0,
    }
    # ТЗ §4.5 п.2: агенты и память на месте.
    for u in (anna, igor):
        n = await _one(
            engine,
            "SELECT count(*) FROM memory_items m JOIN agents a ON a.id = m.agent_id "
            "WHERE a.user_id = :u",
            u=u,
        )
        assert n == 1
    anna2 = await _call(sessionmaker, anna, CREATE)
    igor2 = await _call(sessionmaker, igor, CREATE)
    assert len({couple, anna2, igor2}) == 3
    # Игорь в своей ожидающей паре — чужую ссылку не принять (FB005); вышел — принял.
    t2 = _token()
    await _call(sessionmaker, anna, ISSUE, h=t2)
    await _fails(sessionmaker, igor, "FB005", ACCEPT, h=t2)
    assert await _call(sessionmaker, igor, LEAVE) == igor2
    assert await _call(sessionmaker, igor, ACCEPT, h=t2) == anna2
    assert (await _state(engine, anna2))["status"] == "active"
    assert (await _state(engine, igor2))["status"] == "dissolved"


# --- Срок 7 дней: граница (ТЗ §4.1 п.6) ------------------------------------------------------


async def test_issue_sets_exactly_7_days(sessionmaker: SM, engine: AsyncEngine) -> None:
    uid = await _user(engine, "Срок")
    await _call(sessionmaker, uid, CREATE)
    async with engine.connect() as conn, conn.begin():
        await _as_app(conn, uid)
        expires = (await conn.execute(text(ISSUE), {"h": _token()})).scalar_one()
        exact = (await conn.execute(text("SELECT now() + interval '7 days'"))).scalar_one()
    assert expires == exact


@pytest.mark.parametrize(
    ("offset", "accepted"),
    [
        (timedelta(microseconds=1), True),
        (timedelta(0), False),
        (timedelta(microseconds=-1), False),
    ],
)
async def test_expiry_boundary(
    sessionmaker: SM, engine: AsyncEngine, offset: timedelta, accepted: bool
) -> None:
    """expires_at > now() — действует; expires_at = now() (ровно 7 дней) — уже истекло."""
    _, couple, token = await _pending(sessionmaker, engine, "Граница")
    guest = await _user(engine, "Гость")

    async def attempt(conn: AsyncConnection) -> object:
        async with conn.begin():
            # now() — время начала транзакции; сдвигаем срок относительно него же.
            await conn.execute(
                text("UPDATE invites SET expires_at = now() + :o WHERE token_hash = :h"),
                {"o": offset, "h": token},
            )
            await _as_app(conn, guest)
            return (await conn.execute(text(ACCEPT), {"h": token})).scalar_one()

    async with engine.connect() as conn:
        if accepted:
            assert await attempt(conn) == couple
        else:
            with pytest.raises(DBAPIError) as ei:
                await attempt(conn)
            assert _sqlstate(ei.value) == "FB002"
    expected = "active" if accepted else "pending"
    assert (await _state(engine, couple))["status"] == expected


# --- Перевыпуск и выход инициатора ----------------------------------------------------------


async def test_reissue_revokes_old_link(sessionmaker: SM, engine: AsyncEngine) -> None:
    uid, couple, old = await _pending(sessionmaker, engine, "Перевыпуск")
    new = _token()
    await _call(sessionmaker, uid, ISSUE, h=new)
    assert await _one(engine, "SELECT status FROM invites WHERE token_hash = :h", h=old) == (
        "revoked"
    )
    assert (await _state(engine, couple))["active_invites"] == 1
    guest = await _user(engine, "Гость-П")
    await _fails(sessionmaker, guest, "FB003", ACCEPT, h=old)
    assert await _call(sessionmaker, guest, ACCEPT, h=new) == couple
    # После принятия новая ссылка не выпускается, старая — по-прежнему отозвана.
    await _fails(sessionmaker, uid, "FB006", ISSUE, h=_token())
    await _fails(sessionmaker, await _user(engine, "Третий"), "FB003", ACCEPT, h=old)


async def test_accept_after_initiator_left(sessionmaker: SM, engine: AsyncEngine) -> None:
    uid, couple, token = await _pending(sessionmaker, engine, "Ушла")
    await _call(sessionmaker, uid, LEAVE)
    guest = await _user(engine, "Опоздал")
    await _fails(sessionmaker, guest, "FB003", ACCEPT, h=token)
    assert await _state(engine, couple) == {
        "status": "dissolved",
        "active_members": 0,
        "active_invites": 0,
    }
    n = await _one(engine, "SELECT count(*) FROM couple_members WHERE user_id = :u", u=guest)
    assert n == 0
    # Инициатор после выхода может начать заново, старая ссылка не оживает.
    await _call(sessionmaker, uid, CREATE)
    await _fails(sessionmaker, guest, "FB003", ACCEPT, h=token)


# --- Гонки (блокировки: пользователь → приглашения → пара) ---------------------------------


@pytest.mark.parametrize("first", ["accept", "leave"])
async def test_race_accept_vs_leave(sessionmaker: SM, engine: AsyncEngine, first: str) -> None:
    initiator, couple, token = await _pending(sessionmaker, engine, "Гонка-В")
    guest = await _user(engine, "Гонка-П")
    acc = _try(sessionmaker, guest, ACCEPT, h=token)
    lv = _try(sessionmaker, initiator, LEAVE)
    calls = (acc, lv) if first == "accept" else (lv, acc)
    res = await _race(
        engine, "SELECT 1 FROM invites WHERE token_hash = :h FOR UPDATE", {"h": token}, *calls
    )
    r_acc, r_lv = (res[0], res[1]) if first == "accept" else (res[1], res[0])
    assert r_lv == couple
    assert r_acc == (couple if first == "accept" else "FB003")
    # Итог согласован: пара распущена для всех, ссылок нет, никто не «завис» в паре.
    assert await _state(engine, couple) == {
        "status": "dissolved",
        "active_members": 0,
        "active_invites": 0,
    }
    for u in (initiator, guest):
        await _call(sessionmaker, u, CREATE)


@pytest.mark.parametrize("first", ["accept", "issue"])
async def test_race_accept_vs_reissue(sessionmaker: SM, engine: AsyncEngine, first: str) -> None:
    initiator, couple, old = await _pending(sessionmaker, engine, "Гонка-Р")
    guest = await _user(engine, "Гонка-Г")
    new = _token()
    acc = _try(sessionmaker, guest, ACCEPT, h=old)
    iss = _try(sessionmaker, initiator, ISSUE, h=new)
    calls = (acc, iss) if first == "accept" else (iss, acc)
    res = await _race(
        engine, "SELECT 1 FROM invites WHERE token_hash = :h FOR UPDATE", {"h": old}, *calls
    )
    r_acc, r_iss = (res[0], res[1]) if first == "accept" else (res[1], res[0])
    st = await _state(engine, couple)
    if first == "accept":
        assert r_acc == couple and r_iss == "FB006"
        assert st == {"status": "active", "active_members": 2, "active_invites": 0}
    else:
        assert r_acc == "FB003" and r_iss is not None and not isinstance(r_iss, str)
        assert st == {"status": "pending", "active_members": 1, "active_invites": 1}
        assert await _call(sessionmaker, guest, ACCEPT, h=new) == couple


async def test_race_one_person_two_invites(sessionmaker: SM, engine: AsyncEngine) -> None:
    """ФТ-04: одновременное принятие двух разных ссылок — вступает ровно в одну пару."""
    _, cx, tx = await _pending(sessionmaker, engine, "Икс")
    _, cy, ty = await _pending(sessionmaker, engine, "Игрек")
    who = await _user(engine, "Двоих")
    res = await _race(
        engine,
        "SELECT 1 FROM users WHERE id = :u FOR UPDATE",
        {"u": who},
        _try(sessionmaker, who, ACCEPT, h=tx),
        _try(sessionmaker, who, ACCEPT, h=ty),
    )
    assert res == [cx, "FB005"]
    assert (
        await _one(
            engine, "SELECT count(*) FROM couple_members WHERE user_id = :u AND active", u=who
        )
        == 1
    )
    assert await _state(engine, cy) == {
        "status": "pending",
        "active_members": 1,
        "active_invites": 1,
    }


async def test_race_accept_vs_create_couple(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Гонка с app_create_couple того же человека: вставка участника берёт KEY SHARE на
    строку users, а принятие — FOR UPDATE, поэтому принятие ждёт и даёт различимый FB005."""
    _, couple, token = await _pending(sessionmaker, engine, "Создаёт")
    who = await _user(engine, "Спешит")
    async with user_scope(sessionmaker, who) as s:
        await s.execute(text(CREATE))
        task = asyncio.ensure_future(_try(sessionmaker, who, ACCEPT, h=token))
        await _wait_locked(engine, 1)
    res = await asyncio.wait_for(task, RACE_TIMEOUT)
    assert (await _state(engine, couple))["status"] == "pending"
    assert res == "FB005"


# --- Д7: после выхода история видна обоим, писать нельзя -----------------------------------


async def test_former_members_read_history_but_cannot_write(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    a, couple, token = await _pending(sessionmaker, engine, "Д7-А")
    b = await _user(engine, "Д7-Б")
    await _call(sessionmaker, b, ACCEPT, h=token)
    async with engine.begin() as conn:
        for author, recipient in ((a, b), (b, a)):
            await conn.execute(
                text(
                    "INSERT INTO partner_messages (couple_id, author_id, recipient_id, "
                    "original_enc, adapted_enc, status) VALUES (:k, :a, :b, 'o', 'x', 'delivered')"
                ),
                {"k": couple, "a": author, "b": recipient},
            )
    await _call(sessionmaker, a, LEAVE)
    for u in (a, b):
        assert await _call(sessionmaker, u, "SELECT count(*) FROM partner_messages") == 2
        assert await _call(sessionmaker, u, "SELECT count(*) FROM app_couple_feed(50, NULL)") == 2
        assert (
            await _call(sessionmaker, u, "SELECT status FROM couples WHERE id = :c", c=couple)
            == "dissolved"
        )
        assert (
            await _call(sessionmaker, u, "SELECT count(*) FROM app_couple_members(:c)", c=couple)
            == 2
        )
        # Писать в распавшуюся пару нельзя (ФТ-08: app_send_draft → FA003).
        async with user_scope(sessionmaker, u) as s:
            draft = (
                await s.execute(
                    text(
                        "INSERT INTO message_drafts (agent_id, text_enc) "
                        "SELECT id, 'черновик' FROM agents RETURNING id"
                    )
                )
            ).scalar_one()
        await _fails(sessionmaker, u, "FA003", "SELECT app_send_draft(:d, 1)", d=draft)
        await _fails(sessionmaker, u, "FB007", ISSUE, h=_token())
    # Новый партнёр бывшего участника старую историю не видит.
    await _call(sessionmaker, a, CREATE)
    t2 = _token()
    await _call(sessionmaker, a, ISSUE, h=t2)
    c = await _user(engine, "Д7-В")
    await _call(sessionmaker, c, ACCEPT, h=t2)
    assert await _call(sessionmaker, c, "SELECT count(*) FROM partner_messages") == 0
    assert await _call(sessionmaker, c, "SELECT count(*) FROM couples WHERE id = :k", k=couple) == 0


# --- Б-13: обход функций прямыми UPDATE ----------------------------------------------------


async def _active_couple(sm: SM, engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID, object]:
    a, couple, token = await _pending(sm, engine, "Б13-А")
    b = await _user(engine, "Б13-Б")
    await _call(sm, b, ACCEPT, h=token)
    return a, b, couple


async def test_b13_direct_member_leave_denied(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Б-13 (миграция 0004): прямой UPDATE couple_members от приложения — 42501, пара цела."""
    _, b, couple = await _active_couple(sessionmaker, engine)
    with pytest.raises(DBAPIError) as ei:
        async with user_scope(sessionmaker, b) as s:
            await s.execute(
                text(
                    "UPDATE couple_members SET active = false, left_at = now() "
                    "WHERE user_id = :u AND active"
                ),
                {"u": b},
            )
    assert _sqlstate(ei.value) == "42501", ei.value
    assert "permission denied" in str(ei.value)
    st = await _state(engine, couple)
    assert st == {"status": "active", "active_members": 2, "active_invites": 0}, st


async def test_b13_direct_dissolve_denied(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Б-13 (миграция 0004): прямой UPDATE couples от приложения — 42501, пара цела."""
    a, _, couple = await _active_couple(sessionmaker, engine)
    with pytest.raises(DBAPIError) as ei:
        async with user_scope(sessionmaker, a) as s:
            await s.execute(
                text("UPDATE couples SET status = 'dissolved', dissolved_at = now() WHERE id = :c"),
                {"c": couple},
            )
    assert _sqlstate(ei.value) == "42501", ei.value
    assert "permission denied" in str(ei.value)
    st = await _state(engine, couple)
    assert st == {"status": "active", "active_members": 2, "active_invites": 0}, st


async def test_leave_recovers_inconsistent_couple(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Состояние, которое сейчас достижимо обходом Б-13 (воспроизводим суперпользователем):
    партнёр ушёл прямым UPDATE, пара active с одним участником; инициатор «заперт»
    (новая пара — 23505, чужая ссылка — FB005), но app_leave_couple его освобождает."""
    a, b, couple = await _active_couple(sessionmaker, engine)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE couple_members SET active = false, left_at = now() WHERE user_id = :u"),
            {"u": b},
        )
    assert await _state(engine, couple) == {
        "status": "active",
        "active_members": 1,
        "active_invites": 0,
    }
    await _fails(sessionmaker, a, "23505", CREATE)
    _, _, other = await _pending(sessionmaker, engine, "Б13-Чужая")
    await _fails(sessionmaker, a, "FB005", ACCEPT, h=other)
    assert await _call(sessionmaker, a, LEAVE) == couple
    assert (await _state(engine, couple))["status"] == "dissolved"
    await _call(sessionmaker, a, CREATE)


# --- Вызовы без входа и под чужими ролями (АР-12) ------------------------------------------


@pytest.mark.parametrize(("sql", "token"), [(ISSUE, True), (ACCEPT, True), (LEAVE, False)])
async def test_calls_without_login(
    sessionmaker: SM, engine: AsyncEngine, sql: str, token: bool
) -> None:
    _, couple, t = await _pending(sessionmaker, engine, "Безвхода")
    params = {"h": t} if token else {}
    await _fails(sessionmaker, None, "42501", sql, **params)
    assert await _state(engine, couple) == {
        "status": "pending",
        "active_members": 1,
        "active_invites": 1,
    }


@pytest.mark.parametrize("role", ["feelings_admin", "feelings_worker", "feelings_api"])
@pytest.mark.parametrize("sql", [ISSUE, ACCEPT, LEAVE])
async def test_calls_under_other_roles(
    sessionmaker: SM, engine: AsyncEngine, role: str, sql: str
) -> None:
    """EXECUTE только у feelings_app; feelings_api (NOINHERIT) без SET ROLE — тоже отказ."""
    initiator, couple, token = await _pending(sessionmaker, engine, "Роли")
    guest = await _user(engine, "Роли-Г")
    actor = guest if sql == ACCEPT else initiator
    params = {} if sql == LEAVE else {"h": token if sql == ACCEPT else _token()}
    async with engine.connect() as conn:
        with pytest.raises(DBAPIError) as ei:
            async with conn.begin():
                await _as_app(conn, actor, role)
                await conn.execute(text(sql), params)
    assert _sqlstate(ei.value) == "42501"
    assert "permission denied" in str(ei.value)
    assert (await _state(engine, couple))["status"] == "pending"


async def test_forged_user_id_cannot_join(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Поддельный app.user_id (нет такого пользователя) не вступает в пару."""
    _, couple, token = await _pending(sessionmaker, engine, "Подделка")
    # Раунд 2 (D-3 исправлен в 0003): отказ именно 42501, а не ошибка FK.
    await _fails(sessionmaker, uuid.uuid4(), "42501", ACCEPT, h=token)
    assert await _state(engine, couple) == {
        "status": "pending",
        "active_members": 1,
        "active_invites": 1,
    }


# --- Раунд 2: FB008 — пара только после онбординга (ТЗ §4.1 п.2, п.4, §9.3; АР-13, 0003) ---


async def _unset(engine: AsyncEngine, uid: uuid.UUID, *cols: str) -> None:
    async with engine.begin() as conn:
        sets = ", ".join(f"{c} = NULL" for c in cols)
        await conn.execute(text(f"UPDATE users SET {sets} WHERE id = :u"), {"u": uid})


async def test_fb008_new_user_cannot_create_or_join_until_onboarded(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Только что вошедший (ни согласий, ни 18+) не создаёт пару и не принимает ссылку;
    ссылка остаётся действующей и принимается, как только онбординг пройден."""
    _, couple, token = await _pending(sessionmaker, engine, "FB8-Инициатор")
    newbie = await _user(engine, "FB8-Новичок", onboarded=False)
    await _fails(sessionmaker, newbie, "FB008", CREATE)
    await _fails(sessionmaker, newbie, "FB008", ACCEPT, h=token)
    # Неизвестная ссылка тоже FB008, а не FB001: до онбординга о ссылках ничего не сообщается.
    await _fails(sessionmaker, newbie, "FB008", ACCEPT, h=_token())
    assert await _state(engine, couple) == {
        "status": "pending",
        "active_members": 1,
        "active_invites": 1,
    }
    assert (
        await _one(engine, "SELECT count(*) FROM couple_members WHERE user_id = :u", u=newbie) == 0
    )
    async with engine.begin() as conn:
        stamps = ", ".join(f"{c} = now()" for c in ONBOARDING_COLUMNS)
        await conn.execute(text(f"UPDATE users SET {stamps} WHERE id = :u"), {"u": newbie})
    assert await _call(sessionmaker, newbie, ACCEPT, h=token) == couple
    assert (await _state(engine, couple))["status"] == "active"


@pytest.mark.parametrize("missing", ONBOARDING_COLUMNS)
async def test_fb008_each_missing_item_blocks_accept(
    sessionmaker: SM, engine: AsyncEngine, missing: str
) -> None:
    """Каждое согласие и 18+ обязательны по отдельности (ТЗ §4.1 п.2)."""
    _, couple, token = await _pending(sessionmaker, engine, "FB8-Каждый")
    guest = await _user(engine, "FB8-Гость")
    await _unset(engine, guest, missing)
    await _fails(sessionmaker, guest, "FB008", ACCEPT, h=token)
    await _fails(sessionmaker, guest, "FB008", CREATE)
    assert (await _state(engine, couple))["active_invites"] == 1


async def test_fb008_check_order(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Порядок проверок: 22023 (хеш) → 42501 (нет/удалён) → FB008 (онбординг)."""
    newbie = await _user(engine, "FB8-Порядок", onboarded=False)
    await _fails(sessionmaker, newbie, "22023", ACCEPT, h=b"short")
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": newbie})
    await _fails(sessionmaker, newbie, "42501", ACCEPT, h=_token())
    await _fails(sessionmaker, newbie, "42501", CREATE)


@pytest.mark.parametrize("who", ["partner", "initiator"])
async def test_age_revoked_member_can_still_leave(
    sessionmaker: SM, engine: AsyncEngine, who: str
) -> None:
    """Ответ «мне нет 18» (record_underage снимает age_confirmed_at) не запирает в паре:
    выход работает, пара распускается для обоих, журнал пишет вышедшего; второй участник
    создаёт новую пару, а не подтвердивший возраст — нет (FB008)."""
    initiator, couple, token = await _pending(sessionmaker, engine, "FB8-Выход-И")
    partner = await _user(engine, "FB8-Выход-П")
    await _call(sessionmaker, partner, ACCEPT, h=token)
    leaver, other = (partner, initiator) if who == "partner" else (initiator, partner)
    await _unset(engine, leaver, "age_confirmed_at")
    assert await _call(sessionmaker, leaver, LEAVE) == couple
    assert await _state(engine, couple) == {
        "status": "dissolved",
        "active_members": 0,
        "active_invites": 0,
    }
    actor = await _one(
        engine,
        "SELECT actor_id FROM audit_log WHERE object_id = :c AND action = 'couple.leave'",
        c=couple,
    )
    assert actor == leaver
    await _fails(sessionmaker, leaver, "FB008", CREATE)
    await _call(sessionmaker, other, CREATE)


@pytest.mark.parametrize("outcome", ["commit", "rollback"])
async def test_race_age_revocation_vs_accept(
    sessionmaker: SM, engine: AsyncEngine, outcome: str
) -> None:
    """Отзыв 18+ и принятие одновременно: принятие ждёт строку users и видит итог отзыва
    (зафиксирован — FB008; откатан — вступает)."""
    _, couple, token = await _pending(sessionmaker, engine, "FB8-Гонка-И")
    guest = await _user(engine, "FB8-Гонка-Г")
    async with engine.connect() as holder:
        await holder.execute(
            text("UPDATE users SET age_confirmed_at = NULL WHERE id = :u"), {"u": guest}
        )
        task = asyncio.ensure_future(_try(sessionmaker, guest, ACCEPT, h=token))
        try:
            await _wait_locked(engine, 1)
        finally:
            await (holder.commit() if outcome == "commit" else holder.rollback())
        res = await asyncio.wait_for(task, RACE_TIMEOUT)
    if outcome == "commit":
        assert res == "FB008"
        assert (await _state(engine, couple))["status"] == "pending"
    else:
        assert res == couple
        assert (await _state(engine, couple))["status"] == "active"


async def test_issue_invite_requires_onboarding(sessionmaker: SM, engine: AsyncEngine) -> None:
    uid, couple, _ = await _pending(sessionmaker, engine, "FB8-Ссылка")
    await _unset(engine, uid, "age_confirmed_at")
    await _fails(sessionmaker, uid, "FB008", ISSUE, h=_token())
    # Ни одной новой ссылки (D-4, миграция 0004): активной остаётся только первая.
    assert (await _state(engine, couple))["active_invites"] == 1

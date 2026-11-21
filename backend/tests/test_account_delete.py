"""4.5, этап 1: удаление аккаунта функцией app_delete_account() — миграция 0005.

ФТ-21; ТЗ §4.5 п.3, §7, §9.1 п.5–6; DECISIONS.md АР-13, АР-23; BACKLOG Б-3, Б-19, Б-20.
Данные заводит суперпользователь (`engine`, в обход RLS), действия — под ролью входа
приложения `feelings_api` (`sessionmaker` + `user_scope` → `feelings_app`). Проверки «следов не
осталось» — суперпользователем по всем таблицам схемы. Данные выдуманные (CLAUDE.md)."""

import asyncio
import itertools
import uuid
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import Keyring, generate_key_entry, parse_keyring
from app.db import user_scope
from app.repositories import texts
from tests.conftest import alembic
from tests.test_couple_functions import (
    ACCEPT,
    ONBOARDED,
    SM,
    _call,
    _fails,
    _members,
    _row,
    _sqlstate,
    _token,
    _try,
    _wait_lock_waiters,
)

_tg = itertools.count(8_800_000)
DELETE = "SELECT app_delete_account()"
SEND = "SELECT app_send_draft(:d, 1)"
CREATE = "SELECT app_create_couple()"


@dataclass
class Person:
    user_id: uuid.UUID
    telegram_id: int
    agent_id: uuid.UUID
    draft_id: uuid.UUID
    marker: bytes  # уникальная метка во всех текстах человека


async def _person(engine: AsyncEngine, name: str, *, onboarded: bool = True) -> Person:
    """Пользователь с агентом, диалогом, памятью и черновиком; тексты помечены меткой."""
    uid, aid, cid, did = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    tg = next(_tg)
    marker = f"метка-{uuid.uuid4().hex}".encode()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (id, telegram_id, name) VALUES (:id, :tg, :n)"),
            {"id": uid, "tg": tg, "n": name},
        )
        if onboarded:
            await conn.execute(text(f"UPDATE users SET {ONBOARDED} WHERE id = :u"), {"u": uid})
        await conn.execute(
            text("INSERT INTO agents (id, user_id) VALUES (:a, :u)"), {"a": aid, "u": uid}
        )
        await conn.execute(
            text("INSERT INTO conversations (id, agent_id) VALUES (:c, :a)"),
            {"c": cid, "a": aid},
        )
        await conn.execute(
            text(
                "INSERT INTO conversation_messages (conversation_id, author, text_enc) "
                "VALUES (:c, 'owner', :t)"
            ),
            {"c": cid, "t": b"ispoved " + marker},
        )
        await conn.execute(
            text(
                "INSERT INTO memory_items (agent_id, type, text_enc, source) "
                "VALUES (:a, 'fact', :t, 'owner_dialog')"
            ),
            {"a": aid, "t": b"pamyat " + marker},
        )
        await conn.execute(
            text("INSERT INTO message_drafts (id, agent_id, text_enc) VALUES (:d, :a, :t)"),
            {"d": did, "a": aid, "t": b"chernovik " + marker},
        )
    return Person(uid, tg, aid, did, marker)


async def _couple(sm: SM, initiator: Person, partner: Person) -> tuple[uuid.UUID, bytes]:
    """Активная пара через функции: initiator создаёт и выдаёт ссылку, partner принимает."""
    couple = uuid.UUID(str(await _call(sm, initiator.user_id, CREATE)))
    token = _token()
    await _call(sm, initiator.user_id, "SELECT app_issue_invite(:h)", h=token)
    await _call(sm, partner.user_id, ACCEPT, h=token)
    return couple, token


async def _message(
    engine: AsyncEngine,
    couple: uuid.UUID,
    author: Person,
    recipient: Person,
    body: bytes,
    reply_to: uuid.UUID | None = None,
) -> uuid.UUID:
    mid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO partner_messages (id, couple_id, author_id, recipient_id, "
                "reply_to_id, original_enc, adapted_enc, status) "
                "VALUES (:m, :c, :a, :r, :rt, :t, :t, 'delivered')"
            ),
            {
                "m": mid,
                "c": couple,
                "a": author.user_id,
                "r": recipient.user_id,
                "rt": reply_to,
                "t": body,
            },
        )
    return mid


async def _traces(engine: AsyncEngine, person: Person) -> dict[str, int]:
    """Сколько строк в каждой колонке схемы содержит id, Telegram ID или текстовую метку
    человека. Перебор information_schema: новая таблица попадёт в проверку сама."""
    found: dict[str, int] = {}
    async with engine.connect() as conn:
        cols = (
            await conn.execute(
                text(
                    "SELECT table_name, column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name <> 'alembic_version' "
                    "AND data_type IN ('uuid', 'bigint', 'bytea', 'text', 'character varying')"
                )
            )
        ).all()
        tables = {c.table_name for c in cols}
        assert {"users", "audit_log", "partner_messages", "memory_items"} <= tables
        for table, column, dtype in cols:
            value: object
            if dtype == "uuid":
                cond, value = f'"{column}" = :v', person.user_id
            elif dtype == "bigint":
                cond, value = f'"{column}" = :v', person.telegram_id
            elif dtype == "bytea":
                cond, value = f'position(:v IN "{column}") > 0', person.marker
            else:
                cond, value = f'strpos("{column}", :v) > 0', person.marker.decode()
            n = (
                await conn.execute(
                    text(f'SELECT count(*) FROM "{table}" WHERE {cond}'),
                    {"v": value},
                )
            ).scalar_one()
            if n:
                found[f"{table}.{column}"] = int(n)
    return found


@dataclass
class Scene:
    anna: Person  # удаляет аккаунт
    igor: Person  # партнёр
    couple: uuid.UUID
    anna_msg: uuid.UUID  # Анна → Игорю
    igor_msg: uuid.UUID  # Игорь → Анне
    igor_reply: uuid.UUID  # ответ Игоря на сообщение Анны
    igor_memory_own: uuid.UUID
    subscription: uuid.UUID
    payment: uuid.UUID


async def _scene(sm: SM, engine: AsyncEngine) -> Scene:
    """Анна и Игорь — активная пара (инициатор Игорь, Анна приняла его ссылку — used_by),
    у Анны старая распавшаяся пара с её отозванной ссылкой, переписка в обе стороны, ответ и
    черновик ответа Игоря на сообщение Анны, память Игоря из сообщения Анны, событие
    безопасности, подписка с Анной-плательщиком, записи журнала с её id."""
    anna, igor = await _person(engine, "Анна"), await _person(engine, "Игорь")
    old = await _call(sm, anna.user_id, CREATE)
    await _call(sm, anna.user_id, "SELECT app_issue_invite(:h)", h=_token())
    await _call(sm, anna.user_id, "SELECT app_leave_couple()")  # couple.leave с её actor_id
    couple, _ = await _couple(sm, igor, anna)
    anna_msg = await _message(engine, couple, anna, igor, b"ot anny " + anna.marker)
    igor_msg = await _message(engine, couple, igor, anna, b"ot igorya " + igor.marker)
    igor_reply = await _message(
        engine, couple, igor, anna, b"otvet igorya " + igor.marker, reply_to=anna_msg
    )
    sub, pay = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE message_drafts SET reply_to_id = :m WHERE id = :d"),
            {"m": anna_msg, "d": igor.draft_id},
        )
        await conn.execute(
            text("UPDATE message_drafts SET reply_to_id = :m WHERE id = :d"),
            {"m": igor_msg, "d": anna.draft_id},
        )
        await conn.execute(
            text(
                "INSERT INTO memory_items (agent_id, type, text_enc, source, source_message_id) "
                "VALUES (:a, 'from_partner', :t, 'partner_message', :m)"
            ),
            {"a": igor.agent_id, "t": b"citata " + anna.marker, "m": anna_msg},
        )
        own_mem = (
            await conn.execute(
                text("SELECT id FROM memory_items WHERE agent_id = :a AND type = 'fact'"),
                {"a": igor.agent_id},
            )
        ).scalar_one()
        await conn.execute(
            text("INSERT INTO safety_events (user_id, type) VALUES (:u, 'self_harm')"),
            {"u": anna.user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO subscriptions (id, couple_id, payer_id, plan, status, period_start, "
                "period_end) VALUES (:s, :c, :u, 'month', 'active', now(), now() + "
                "interval '30 days')"
            ),
            {"s": sub, "c": couple, "u": anna.user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO payments (id, subscription_id, amount, provider, status) "
                "VALUES (:p, :s, 100, 'telegram_stars', 'succeeded')"
            ),
            {"p": pay, "s": sub},
        )
        await conn.execute(
            text(
                "INSERT INTO audit_log (actor_id, action, object_type, object_id) "
                "VALUES (NULL, 'user.update', 'user', :u)"
            ),
            {"u": anna.user_id},
        )
    assert old != couple
    return Scene(anna, igor, couple, anna_msg, igor_msg, igor_reply, own_mem, sub, pay)


# --- Следов не остаётся --------------------------------------------------------------------


async def test_delete_leaves_no_row_with_user_id(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Критерий 1: после удаления ни в одной колонке ни одной таблицы нет id, Telegram ID и
    текстов удалённого; журнал — одна обезличенная запись account.delete (АР-23)."""
    sc = await _scene(sessionmaker, engine)
    before = await _traces(engine, sc.anna)
    # Сцена действительно затрагивает все связи с пользователем.
    for col in (
        "users.id",
        "users.telegram_id",
        "agents.user_id",
        "couple_members.user_id",
        "invites.created_by",
        "invites.used_by",
        "partner_messages.author_id",
        "partner_messages.recipient_id",
        "safety_events.user_id",
        "subscriptions.payer_id",
        "audit_log.actor_id",
        "audit_log.object_id",
        "conversation_messages.text_enc",
        "memory_items.text_enc",
        "message_drafts.text_enc",
        "partner_messages.original_enc",
    ):
        assert before.get(col), (col, before)
    async with engine.connect() as conn:
        audit_before = (
            await conn.execute(
                text("SELECT count(*) FROM audit_log WHERE action = 'account.delete'")
            )
        ).scalar_one()

    assert await _call(sessionmaker, sc.anna.user_id, DELETE) is None

    assert await _traces(engine, sc.anna) == {}
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT actor_id, object_type, object_id FROM audit_log "
                    "WHERE action = 'account.delete' ORDER BY id"
                )
            )
        ).all()
    assert len(rows) == audit_before + 1
    assert tuple(rows[-1]) == (None, "user", None)
    # Агент, диалог, память и черновики партнёра на месте (кроме памяти из её сообщения).
    assert await _traces(engine, sc.igor) != {}


async def test_partner_keeps_own_messages_and_is_free(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Критерий 2: пара распущена; сообщения Игоря Анне остаются у него (recipient_id → NULL),
    её сообщения удалены; ссылки на них сняты; Игорь создаёт новую пару (ТЗ §4.5 п.2–3)."""
    sc = await _scene(sessionmaker, engine)
    await _call(sessionmaker, sc.anna.user_id, DELETE)

    c = await _row(engine, "SELECT status, dissolved_at FROM couples WHERE id = :c", c=sc.couple)
    assert c["status"] == "dissolved" and c["dissolved_at"] is not None
    members = await _members(engine, sc.couple)
    assert list(members) == [sc.igor.user_id]
    assert members[sc.igor.user_id]["active"] is False
    async with engine.connect() as conn:
        msgs = {
            r.id: (r.author_id, r.recipient_id, r.reply_to_id)
            for r in await conn.execute(
                text(
                    "SELECT id, author_id, recipient_id, reply_to_id FROM partner_messages "
                    "WHERE couple_id = :c"
                ),
                {"c": sc.couple},
            )
        }
        draft_reply = (
            await conn.execute(
                text("SELECT reply_to_id FROM message_drafts WHERE id = :d"),
                {"d": sc.igor.draft_id},
            )
        ).scalar_one()
        memory = {
            r[0]
            for r in await conn.execute(
                text("SELECT id FROM memory_items WHERE agent_id = :a"), {"a": sc.igor.agent_id}
            )
        }
        sub = (
            await conn.execute(
                text(
                    "SELECT s.payer_id, count(p.id) FROM subscriptions s "
                    "JOIN payments p ON p.subscription_id = s.id WHERE s.id = :s GROUP BY 1"
                ),
                {"s": sc.subscription},
            )
        ).one()
    assert msgs == {
        sc.igor_msg: (sc.igor.user_id, None, None),
        sc.igor_reply: (sc.igor.user_id, None, None),
    }
    assert draft_reply is None
    assert memory == {sc.igor_memory_own}  # из сообщения Анны — удалена, своя — на месте
    assert tuple(sub) == (None, 1)  # подписка и платёж пары остаются, плательщик обезличен

    # Партнёр видит свою историю (Д7) — только свои сообщения — и свободен для новой пары.
    async with user_scope(sessionmaker, sc.igor.user_id) as s:
        feed = (await s.execute(text("SELECT id, outgoing FROM app_couple_feed(100, NULL)"))).all()
        assert {(r.id, r.outgoing) for r in feed} == {(sc.igor_msg, True), (sc.igor_reply, True)}
        agents = (await s.execute(text("SELECT count(*) FROM agents"))).scalar_one()
        assert agents == 1
    await _fails(sessionmaker, sc.igor.user_id, "FA003", SEND, d=sc.igor.draft_id)
    new = await _call(sessionmaker, sc.igor.user_id, CREATE)
    assert new != sc.couple


async def test_pending_couple_invite_dead_after_delete(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Б-20: приглашения удалённого автора больше не находятся и не принимаются; ожидавшая
    партнёра пара, в которой не осталось участников, удаляется с подпиской и платежами."""
    vera = await _person(engine, "Вера")
    couple = await _call(sessionmaker, vera.user_id, CREATE)
    token = _token()
    await _call(sessionmaker, vera.user_id, "SELECT app_issue_invite(:h)", h=token)
    sub = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO subscriptions (id, couple_id, payer_id, plan, status, period_start, "
                "period_end) VALUES (:s, :c, :u, 'month', 'trial', now(), now() + "
                "interval '7 days')"
            ),
            {"s": sub, "c": couple, "u": vera.user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO payments (subscription_id, amount, provider, status) "
                "VALUES (:s, 1, 'telegram_stars', 'pending')"
            ),
            {"s": sub},
        )
    guest = await _person(engine, "Гость")
    async with user_scope(sessionmaker, guest.user_id) as s:
        n = await s.execute(text("SELECT count(*) FROM app_find_invite(:h)"), {"h": token})
        assert n.scalar_one() == 1

    await _call(sessionmaker, vera.user_id, DELETE)

    async with user_scope(sessionmaker, guest.user_id) as s:
        n = await s.execute(text("SELECT count(*) FROM app_find_invite(:h)"), {"h": token})
        assert n.scalar_one() == 0
    await _fails(sessionmaker, guest.user_id, "FB001", ACCEPT, h=token)
    # Пара без участников удалена вместе с подпиской и платежом (АР-23).
    left = await _row(
        engine,
        "SELECT (SELECT count(*) FROM couples WHERE id = :c) AS couples, "
        "(SELECT count(*) FROM subscriptions WHERE couple_id = :c) AS subs, "
        "(SELECT count(*) FROM payments WHERE subscription_id = :s) AS payments, "
        "(SELECT count(*) FROM couples c WHERE NOT EXISTS "
        " (SELECT 1 FROM couple_members cm WHERE cm.couple_id = c.id)) AS orphans",
        c=couple,
        s=sub,
    )
    assert left == {"couples": 0, "subs": 0, "payments": 0, "orphans": 0}
    assert await _traces(engine, vera) == {}


async def test_delete_without_couple_and_without_onboarding(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Удалить аккаунт можно не в паре и до завершения онбординга (ФТ-21, как выход — АР-17)."""
    for onboarded in (True, False):
        p = await _person(engine, "Одиночка", onboarded=onboarded)
        await _call(sessionmaker, p.user_id, DELETE)
        assert await _traces(engine, p) == {}


async def test_delete_only_self_and_errors(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Функция действует только на app.user_id: без входа, неизвестный, помеченный удалённым
    и уже удалённый — 42501; посторонний пользователь не затронут."""
    await _fails(sessionmaker, None, "42501", DELETE)
    await _fails(sessionmaker, uuid.uuid4(), "42501", DELETE)
    soft = await _person(engine, "Помеченный")
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": soft.user_id}
        )
    await _fails(sessionmaker, soft.user_id, "42501", DELETE)
    gone, bystander = await _person(engine, "Уходящий"), await _person(engine, "Сосед")
    await _call(sessionmaker, gone.user_id, DELETE)
    await _fails(sessionmaker, gone.user_id, "42501", DELETE)
    assert set(await _traces(engine, bystander)) >= {"users.id", "agents.user_id"}


# --- Б-19: прямого DELETE нет -------------------------------------------------------------


async def test_direct_delete_of_users_denied(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Критерий 3: у приложения нет DELETE на users (permission denied), политика для DELETE
    отсутствует; чтение и правка своего профиля работают."""
    p = await _person(engine, "Прямой")
    with pytest.raises(DBAPIError) as ei:
        await _call(sessionmaker, p.user_id, "DELETE FROM users WHERE id = app_uid() RETURNING 1")
    assert _sqlstate(ei.value) == "42501" and "permission denied" in str(ei.value)
    async with user_scope(sessionmaker, p.user_id) as s:
        upd = await s.execute(text("UPDATE users SET name = 'Новое' WHERE id = app_uid()"))
        assert upd.rowcount == 1  # type: ignore[attr-defined]
        assert (await s.execute(text("SELECT count(*) FROM users"))).scalar_one() == 1
    async with engine.connect() as conn:
        priv = await conn.execute(
            text(
                "SELECT has_table_privilege('feelings_app', 'users', 'DELETE'), "
                "has_table_privilege('feelings_app', 'users', 'INSERT'), "
                "has_table_privilege('feelings_app', 'users', 'SELECT')"
            )
        )
        assert tuple(priv.one()) == (False, False, True)
        policies = await conn.execute(
            text("SELECT policyname, cmd FROM pg_policies WHERE tablename = 'users'")
        )
        assert sorted(map(tuple, policies)) == [
            ("users_select", "SELECT"),
            ("users_update", "UPDATE"),
        ]
    assert "users.id" in await _traces(engine, p)


async def test_execute_only_for_app_role(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        for role, allowed in (
            ("feelings_app", True),
            ("feelings_api", False),
            ("feelings_worker", False),
            ("feelings_admin", False),
            ("public", False),
        ):
            row = await conn.execute(
                text("SELECT has_function_privilege(:r, 'app_delete_account()', 'EXECUTE')"),
                {"r": role},
            )
            assert row.scalar_one() is allowed, role
        sec = await conn.execute(
            text(
                "SELECT prosecdef, proconfig FROM pg_proc WHERE oid = "
                "'app_delete_account()'::regprocedure"
            )
        )
        definer, config = sec.one()
        assert definer is True and config == ["search_path=public, pg_temp"]


# --- Б-3: повторная регистрация ------------------------------------------------------------


async def test_relogin_creates_new_user(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Критерий 4: вход тем же Telegram ID после удаления — новый пользователь без следов."""
    sc = await _scene(sessionmaker, engine)
    await _call(sessionmaker, sc.anna.user_id, DELETE)
    async with user_scope(sessionmaker, None) as s:
        new_id = (
            await s.execute(text("SELECT app_upsert_user(:t)"), {"t": sc.anna.telegram_id})
        ).scalar_one()
    assert new_id is not None and new_id != sc.anna.user_id
    assert await _traces(engine, sc.anna) == {"users.telegram_id": 1}
    new = await _row(
        engine,
        "SELECT name, consent_terms_at, age_confirmed_at, deleted_at FROM users WHERE id = :u",
        u=new_id,
    )
    assert set(new.values()) == {None}
    async with user_scope(sessionmaker, new_id) as s:
        for table in ("agents", "memory_items", "message_drafts", "couples", "partner_messages"):
            n = (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
            assert n == 0, table
        feed = await s.execute(text("SELECT count(*) FROM app_couple_feed(100, NULL)"))
        assert feed.scalar_one() == 0


# --- Гонки (АР-13: пользователь → приглашения → пара) --------------------------------------


async def _hold_and_race(
    engine: AsyncEngine,
    lock_sql: str,
    params: dict[str, object],
    steps: Sequence[tuple[str, Coroutine[Any, Any, object]]],
) -> list[object]:
    """Третья транзакция держит строку; вызовы запускаются по очереди, каждый — после того,
    как предыдущие встали в ожидание блокировки; затем блокировка снимается. Так порядок
    вызовов задан, а не случаен. steps — (имя функции для ожидания, корутина)."""
    async with engine.connect() as holder:
        await holder.execute(text(lock_sql), params)
        tasks: list[asyncio.Future[object]] = []
        try:
            for func, coro in steps:
                tasks.append(asyncio.ensure_future(coro))
                await _wait_lock_waiters(engine, func, 1)
        finally:
            await holder.rollback()
        return list(await asyncio.wait_for(asyncio.gather(*tasks), timeout=30))


@pytest.mark.parametrize("first", ["delete", "accept"])
async def test_race_delete_vs_accept_invite(
    sessionmaker: SM, engine: AsyncEngine, first: str
) -> None:
    """Автор ссылки удаляет аккаунт, пока гость её принимает: без взаимоблокировки (40P01).
    Удаление первым — гость получает FB001 (ссылки нет); принятие первым — гость вступает,
    затем пара распускается удалением, гость свободен."""
    vera = await _person(engine, "Вера")
    couple = uuid.UUID(str(await _call(sessionmaker, vera.user_id, CREATE)))
    token = _token()
    await _call(sessionmaker, vera.user_id, "SELECT app_issue_invite(:h)", h=token)
    guest = await _person(engine, "Гость")
    delete = ("app_delete_account", _try(sessionmaker, vera.user_id, DELETE))
    accept = ("app_accept_invite", _try(sessionmaker, guest.user_id, ACCEPT, h=token))
    steps = [delete, accept] if first == "delete" else [accept, delete]
    got = dict(
        zip(
            [s[0] for s in steps],
            await _hold_and_race(
                engine, "SELECT 1 FROM couples WHERE id = :c FOR UPDATE", {"c": couple}, steps
            ),
            strict=True,
        )
    )
    assert got["app_delete_account"] is None
    assert got["app_accept_invite"] == ("FB001" if first == "delete" else couple)
    assert await _traces(engine, vera) == {}
    members = await _members(engine, couple)
    async with engine.connect() as conn:
        status = (
            await conn.execute(text("SELECT status FROM couples WHERE id = :c"), {"c": couple})
        ).scalar_one_or_none()
    if first == "delete":  # пара без участников удалена
        assert (status, members) == (None, {})
    else:  # гость успел вступить: пара распущена, он — бывший участник
        assert status == "dissolved"
        assert list(members) == [guest.user_id] and members[guest.user_id]["active"] is False
    assert await _call(sessionmaker, guest.user_id, CREATE) != couple


@pytest.mark.parametrize("first", ["delete", "send"])
async def test_race_delete_recipient_vs_send(
    sessionmaker: SM, engine: AsyncEngine, first: str
) -> None:
    """Получатель удаляет аккаунт, пока партнёр нажимает «Отправить» (ФТ-08): без 40P01.
    Удаление первым — FA003, черновик не отправлен; отправка первой — сообщение остаётся у
    автора с recipient_id = NULL."""
    anna, igor = await _person(engine, "Анна"), await _person(engine, "Игорь")
    couple, _ = await _couple(sessionmaker, anna, igor)
    delete = ("app_delete_account", _try(sessionmaker, anna.user_id, DELETE))
    send = ("app_send_draft", _try(sessionmaker, igor.user_id, SEND, d=igor.draft_id))
    if first == "delete":  # удаление ждёт пару (держатель), отправка — строку Анны
        lock, params, steps = (
            "SELECT 1 FROM couples WHERE id = :c FOR UPDATE",
            couple,
            [
                delete,
                send,
            ],
        )
    else:  # отправка ждёт свой черновик (держатель), удаление — ключевую блокировку Анны
        lock, params, steps = (
            "SELECT 1 FROM message_drafts WHERE id = :c FOR UPDATE",
            igor.draft_id,
            [send, delete],
        )
    results = await _hold_and_race(engine, lock, {"c": params}, steps)
    got = dict(zip([s[0] for s in steps], results, strict=True))
    assert got["app_delete_account"] is None
    assert await _traces(engine, anna) == {}
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT m.id, m.author_id, m.recipient_id, d.status FROM message_drafts d "
                    "LEFT JOIN partner_messages m ON m.draft_id = d.id WHERE d.id = :d"
                ),
                {"d": igor.draft_id},
            )
        ).one()
    if first == "delete":
        assert got["app_send_draft"] == "FA003"
        assert tuple(rows) == (None, None, None, "draft")
    else:
        assert isinstance(got["app_send_draft"], uuid.UUID)
        assert tuple(rows) == (got["app_send_draft"], igor.user_id, None, "sent")


async def test_race_author_sends_and_deletes(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Автор отправляет с одного устройства и удаляет аккаунт с другого: удаление ждёт
    отправку и удаляет и новое сообщение (его данные)."""
    anna, igor = await _person(engine, "Анна"), await _person(engine, "Игорь")
    await _couple(sessionmaker, anna, igor)
    steps = [
        ("app_send_draft", _try(sessionmaker, anna.user_id, SEND, d=anna.draft_id)),
        ("app_delete_account", _try(sessionmaker, anna.user_id, DELETE)),
    ]
    sent, deleted = await _hold_and_race(
        engine,
        "SELECT 1 FROM message_drafts WHERE id = :d FOR UPDATE",
        {"d": anna.draft_id},
        steps,
    )
    assert isinstance(sent, uuid.UUID) and deleted is None
    assert await _traces(engine, anna) == {}


async def test_race_both_partners_delete(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Оба партнёра удаляют аккаунты одновременно: второй, дождавшись пары, видит её уже
    распущенной (FB007 внутри — не ошибка), оба удалены, в журнале два account.delete."""
    anna, igor = await _person(engine, "Анна"), await _person(engine, "Игорь")
    couple, _ = await _couple(sessionmaker, anna, igor)
    async with engine.connect() as conn:
        n0 = (
            await conn.execute(
                text("SELECT count(*) FROM audit_log WHERE action = 'account.delete'")
            )
        ).scalar_one()
    async with engine.connect() as holder:
        await holder.execute(text("SELECT 1 FROM couples WHERE id = :c FOR UPDATE"), {"c": couple})
        tasks = asyncio.gather(
            _try(sessionmaker, anna.user_id, DELETE), _try(sessionmaker, igor.user_id, DELETE)
        )
        try:
            await _wait_lock_waiters(engine, "app_delete_account", 2)
        finally:
            await holder.rollback()
        results = list(await asyncio.wait_for(tasks, timeout=30))
    assert results == [None, None]
    assert await _traces(engine, anna) == {} and await _traces(engine, igor) == {}
    assert await _members(engine, couple) == {}
    async with engine.connect() as conn:
        gone = await conn.execute(text("SELECT count(*) FROM couples WHERE id = :c"), {"c": couple})
        assert gone.scalar_one() == 0  # второй удалившийся убирает опустевшую пару
        n1 = (
            await conn.execute(
                text("SELECT count(*) FROM audit_log WHERE action = 'account.delete'")
            )
        ).scalar_one()
        leaves = (
            await conn.execute(
                text(
                    "SELECT count(*), count(actor_id) FROM audit_log "
                    "WHERE object_id = :c AND action = 'couple.leave'"
                ),
                {"c": couple},
            )
        ).one()
    assert n1 == n0 + 2
    assert tuple(leaves) == (1, 0)  # выход записан один раз и обезличен


# --- Миграция обратима ---------------------------------------------------------------------


async def test_migration_0005_reversible(engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    """0005 → 0004 → 0005: downgrade возвращает DELETE на users и agents, политики users_self,
    agents_owner и drafts_delete из 0001, app_send_draft из 0001 и убирает app_delete_account;
    повторный upgrade снова их меняет."""

    async def state() -> tuple[object, ...]:
        async with engine.connect() as conn:
            row = await conn.execute(
                text(
                    "SELECT has_table_privilege('feelings_app', 'users', 'DELETE'), "
                    "has_table_privilege('feelings_app', 'agents', 'DELETE'), "
                    "ARRAY(SELECT tablename || '.' || policyname FROM pg_policies "
                    "      WHERE tablename IN ('users', 'agents') ORDER BY 1), "
                    "(SELECT position('sent' IN qual) > 0 FROM pg_policies "
                    " WHERE policyname = 'drafts_delete'), "
                    "to_regprocedure('app_delete_account()') IS NOT NULL, "
                    "position('FOR KEY SHARE' IN "
                    "pg_get_functiondef('app_send_draft(uuid, integer)'::regprocedure)) > 0"
                )
            )
            return tuple(row.one())

    upgraded = (
        False,
        False,
        [
            "agents.agents_insert",
            "agents.agents_select",
            "agents.agents_update",
            "users.users_select",
            "users.users_update",
        ],
        True,
        True,
        True,
    )
    try:
        assert await state() == upgraded
        alembic("downgrade", "0004")
        await engine.dispose()
        assert await state() == (
            True,
            True,
            ["agents.agents_owner", "users.users_self"],
            False,
            False,
            False,
        )
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    assert await state() == upgraded


# --- Б-38: оригинал не теряет контекст черновика -------------------------------------------


async def _send_encrypted(
    sm: SM, keys: Keyring, author: Person, body: str, reply_to: uuid.UUID | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    """Черновик через слой доступа (шифрование 4.2) и «Отправить»: (draft_id, message_id)."""
    draft = await texts.create_draft(sm, keys, author.user_id, author.agent_id, body, reply_to)
    return draft, await texts.send_draft(sm, author.user_id, draft, 1)


async def test_sent_draft_and_agent_not_deletable(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Б-38 (ревью 4.2 MAJOR-1): отправленный черновик и агента приложение удалить не может —
    оригинал остаётся расшифровываемым; неотправленный черновик удаляется как раньше."""
    keys = parse_keyring(generate_key_entry("k1"), "k1")
    anna, igor = await _person(engine, "Анна"), await _person(engine, "Игорь")
    await _couple(sessionmaker, anna, igor)
    sent, msg = await _send_encrypted(sessionmaker, keys, anna, "мне важно")
    async with user_scope(sessionmaker, anna.user_id) as s:
        res = await s.execute(text("DELETE FROM message_drafts WHERE id = :d"), {"d": sent})
        assert res.rowcount == 0  # type: ignore[attr-defined]
        res = await s.execute(
            text("DELETE FROM message_drafts WHERE id = :d"), {"d": anna.draft_id}
        )
        assert res.rowcount == 1  # type: ignore[attr-defined]
    with pytest.raises(DBAPIError) as ei:
        await _call(sessionmaker, anna.user_id, "DELETE FROM agents RETURNING 1")
    assert _sqlstate(ei.value) == "42501" and "permission denied" in str(ei.value)
    assert await texts.read_original(sessionmaker, keys, anna.user_id, msg) == "мне важно"
    feed = await texts.read_feed(sessionmaker, keys, anna.user_id)
    assert [(f.id, f.text) for f in feed] == [(msg, "мне важно")]
    async with engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT has_table_privilege('feelings_app', 'agents', 'DELETE'), "
                "has_table_privilege('feelings_app', 'agents', 'INSERT'), "
                "has_table_privilege('feelings_app', 'agents', 'UPDATE'), "
                "has_table_privilege('feelings_app', 'message_drafts', 'DELETE')"
            )
        )
        assert tuple(row.one()) == (False, True, True, True)
    # Профиль агента по-прежнему создаётся и правится владельцем.
    async with user_scope(sessionmaker, anna.user_id) as s:
        upd = await s.execute(
            text("UPDATE agents SET profile_summary_enc = NULL WHERE user_id = app_uid()")
        )
        assert upd.rowcount == 1  # type: ignore[attr-defined]


async def test_delete_account_keeps_partner_originals_readable(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Б-38 + АР-23: агент и черновики удаляемого уходят каскадом вместе с его сообщениями;
    сообщения партнёра ссылаются на черновики партнёра — их оригиналы читаются, лента цела."""
    keys = parse_keyring(generate_key_entry("k1"), "k1")
    anna, igor = await _person(engine, "Анна"), await _person(engine, "Игорь")
    await _couple(sessionmaker, anna, igor)
    _, anna_msg = await _send_encrypted(sessionmaker, keys, anna, "от Анны")
    async with engine.begin() as conn:  # конвейер доставил сообщение Анны (ответить можно)
        await conn.execute(
            text("UPDATE partner_messages SET status = 'delivered' WHERE id = :m"), {"m": anna_msg}
        )
    igor_draft, igor_msg = await _send_encrypted(sessionmaker, keys, igor, "от Игоря")
    reply_draft, reply_msg = await _send_encrypted(
        sessionmaker, keys, igor, "ответ Игоря", reply_to=anna_msg
    )

    await _call(sessionmaker, anna.user_id, DELETE)

    assert await _traces(engine, anna) == {}
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT m.id, m.draft_id, a.user_id AS draft_owner, m.reply_to_id "
                    "FROM partner_messages m JOIN message_drafts d ON d.id = m.draft_id "
                    "JOIN agents a ON a.id = d.agent_id WHERE m.author_id = :i"
                ),
                {"i": igor.user_id},
            )
        ).all()
        orphaned = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM partner_messages WHERE draft_id IS NULL "
                    "AND (author_id = :i OR recipient_id IS NULL AND couple_id IN "
                    "(SELECT couple_id FROM couple_members WHERE user_id = :i))"
                ),
                {"i": igor.user_id},
            )
        ).scalar_one()
    assert {tuple(r) for r in rows} == {
        (igor_msg, igor_draft, igor.user_id, None),
        (reply_msg, reply_draft, igor.user_id, None),
    }
    assert orphaned == 0
    for msg, body in ((igor_msg, "от Игоря"), (reply_msg, "ответ Игоря")):
        assert await texts.read_original(sessionmaker, keys, igor.user_id, msg) == body
    feed = await texts.read_feed(sessionmaker, keys, igor.user_id)
    assert {(f.id, f.outgoing, f.text) for f in feed} == {
        (igor_msg, True, "от Игоря"),
        (reply_msg, True, "ответ Игоря"),
    }

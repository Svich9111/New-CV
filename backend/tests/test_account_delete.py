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
    embedding: str  # текст уникального эмбеддинга его памяти (как его выводит pgvector)


def _vector(seed: uuid.UUID) -> str:
    """Уникальный эмбеддинг: первые 16 компонент — байты uuid / 256 (точны во float4)."""
    head = [b / 256 for b in seed.bytes]
    return "[" + ",".join(str(v) for v in head + [0.0] * (1024 - len(head))) + "]"


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
        embedding = (
            await conn.execute(text("SELECT CAST(:v AS vector)::text"), {"v": _vector(uid)})
        ).scalar_one()
        if onboarded:
            await conn.execute(text(f"UPDATE users SET {ONBOARDED} WHERE id = :u"), {"u": uid})
        await conn.execute(
            text(
                "INSERT INTO agents (id, user_id, tone_settings) "
                "VALUES (:a, :u, jsonb_build_object('note', CAST(:m AS text)))"
            ),
            {"a": aid, "u": uid, "m": marker.decode()},
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
                "INSERT INTO memory_items (agent_id, type, text_enc, source, embedding) "
                "VALUES (:a, 'fact', :t, 'owner_dialog', CAST(:e AS vector))"
            ),
            {"a": aid, "t": b"pamyat " + marker, "e": embedding},
        )
        await conn.execute(
            text("INSERT INTO message_drafts (id, agent_id, text_enc) VALUES (:d, :a, :t)"),
            {"d": did, "a": aid, "t": b"chernovik " + marker},
        )
    return Person(uid, tg, aid, did, marker, embedding)


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
    """Сколько строк в каждой колонке схемы содержит id, Telegram ID, текстовую метку или
    эмбеддинг человека. Перебор information_schema по всем колонкам любого типа: bytea — по
    байтам метки, остальные (uuid, числа, jsonb, массивы, vector, …) — по CAST(... AS text).
    Новая таблица или колонка попадёт в проверку сама (ревью 4.5 MINOR-5)."""
    found: dict[str, int] = {}
    async with engine.connect() as conn:
        cols = (
            await conn.execute(
                text(
                    "SELECT c.table_name, c.column_name, c.data_type "
                    "FROM information_schema.columns c JOIN information_schema.tables t "
                    "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
                    "WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE' "
                    "AND c.table_name <> 'alembic_version'"
                )
            )
        ).all()
        tables = {c.table_name for c in cols}
        assert {"users", "audit_log", "partner_messages", "memory_items", "agents"} <= tables
        assert ("memory_items", "embedding", "USER-DEFINED") in {tuple(c) for c in cols}
        params: dict[str, object] = {
            "uid": str(person.user_id),
            "tg": rf"(^|[^0-9]){person.telegram_id}([^0-9]|$)",
            "mark": person.marker.decode(),
            "emb": person.embedding,
        }
        for table, column, dtype in cols:
            values: dict[str, object]
            if dtype == "bytea":
                cond, values = f'position(:v IN "{column}") > 0', {"v": person.marker}
            else:
                t = f'CAST("{column}" AS text)'
                cond = (
                    f"(strpos({t}, :uid) > 0 OR {t} ~ :tg OR strpos({t}, :mark) > 0 "
                    f"OR strpos({t}, :emb) > 0)"
                )
                values = params
            n = (
                await conn.execute(text(f'SELECT count(*) FROM "{table}" WHERE {cond}'), values)
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
                "INSERT INTO memory_items (agent_id, type, text_enc, source, source_message_id, "
                "embedding) VALUES (:a, 'from_partner', :t, 'partner_message', :m, "
                "CAST(:e AS vector))"
            ),
            {"a": igor.agent_id, "t": b"citata " + anna.marker, "m": anna_msg, "e": anna.embedding},
        )
        # Версия для Анны (агент получателя адаптировал сообщение Игоря; ТЗ §6.2 п.4–6).
        await conn.execute(
            text(
                "UPDATE partner_messages SET is_adapted = true, tone_score = 2, "
                "harsh_tone_warning = true WHERE id IN (:a, :b)"
            ),
            {"a": igor_msg, "b": igor_reply},
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
        "memory_items.embedding",
        "agents.tone_settings",
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
    # Ревью 4.5 MINOR-3: версии для удалённого получателя нет, оригинал автора на месте.
    async with engine.connect() as conn:
        recipient_side = {
            tuple(r)
            for r in await conn.execute(
                text(
                    "SELECT adapted_enc, is_adapted, tone_score, harsh_tone_warning, "
                    "original_enc FROM partner_messages WHERE couple_id = :c"
                ),
                {"c": sc.couple},
            )
        }
    assert recipient_side == {
        (None, False, None, False, b"ot igorya " + sc.igor.marker),
        (None, False, None, False, b"otvet igorya " + sc.igor.marker),
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


# --- Порядок «пара → сообщения»: удаление против конвейера и двух удалений (MAJOR-1) ------


async def _worker_try(engine: AsyncEngine, sql: str, **params: object) -> object:
    """Вызов функции конвейера под feelings_worker (задача 3.2): результат или SQLSTATE."""
    try:
        async with engine.connect() as conn, conn.begin():
            await conn.execute(text("SET LOCAL ROLE feelings_worker"))
            return (await conn.execute(text(sql), params)).scalar_one()
    except DBAPIError as exc:
        return _sqlstate(exc)


DELIVER = "SELECT pipeline_deliver(:m, '\\x01'::bytea, true, 2::smallint, true)"
WITHHOLD = "SELECT pipeline_withhold(:m)"


async def _cross(
    sm: SM, engine: AsyncEngine
) -> tuple[Person, Person, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """X (Анна) ← Y (ответ Игоря на X) ← Z (ответ Анны на Y, ещё в конвейере)."""
    anna, igor = await _person(engine, "Анна"), await _person(engine, "Игорь")
    couple, _ = await _couple(sm, anna, igor)
    x = await _message(engine, couple, anna, igor, b"x " + anna.marker)
    y = await _message(engine, couple, igor, anna, b"y " + igor.marker, reply_to=x)
    z = await _message(engine, couple, anna, igor, b"z " + anna.marker, reply_to=y)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE partner_messages SET status = 'processing' WHERE id = :z"), {"z": z}
        )
    return anna, igor, couple, x, y, z


@pytest.mark.parametrize("first", ["delete", "deliver"])
async def test_race_delete_vs_pipeline_delivering_own_reply(
    sessionmaker: SM, engine: AsyncEngine, first: str
) -> None:
    """Ревью 4.5 MAJOR-1 (а): Анна удаляет аккаунт, пока конвейер доставляет её ответ Z на
    ответ Игоря Y. Раньше удаление брало Y → Z, доставка Z → Y: 40P01. Теперь обе сначала
    берут пару (удаление FOR UPDATE, конвейер FOR KEY SHARE) и идут по очереди.
    Удаление первым — доставка false (Z удалено); доставка первой — Y «отвечено», затем Z
    удаляется вместе с аккаунтом. Держатель строки Y задаёт порядок: первый встаёт на Y, второй
    — на паре."""
    anna, igor, couple, _x, y, z = await _cross(sessionmaker, engine)
    delete = ("app_delete_account", _try(sessionmaker, anna.user_id, DELETE))
    deliver = ("pipeline_deliver", _worker_try(engine, DELIVER, m=z))
    steps = [delete, deliver] if first == "delete" else [deliver, delete]
    got = dict(
        zip(
            [st[0] for st in steps],
            await _hold_and_race(
                engine, "SELECT 1 FROM partner_messages WHERE id = :y FOR UPDATE", {"y": y}, steps
            ),
            strict=True,
        )
    )
    assert got == {"app_delete_account": None, "pipeline_deliver": first == "deliver"}
    assert await _traces(engine, anna) == {}
    row = await _row(
        engine,
        "SELECT author_id, recipient_id, reply_to_id, status::text AS status, adapted_enc "
        "FROM partner_messages WHERE id = :y",
        y=y,
    )
    assert row == {
        "author_id": igor.user_id,
        "recipient_id": None,
        "reply_to_id": None,
        "status": "answered" if first == "deliver" else "delivered",
        "adapted_enc": None,
    }
    assert list(await _members(engine, couple)) == [igor.user_id]


@pytest.mark.parametrize("dissolved", [True, False], ids=["former", "active"])
@pytest.mark.parametrize("first", ["anna", "igor"])
async def test_race_former_partners_delete_with_cross_replies(
    sessionmaker: SM, engine: AsyncEngine, first: str, dissolved: bool
) -> None:
    """Ревью 4.5 MAJOR-1 (б): пара распалась раньше, в истории встречные ответы; бывшие
    партнёры (и, для сравнения, партнёры активной пары) удаляют аккаунты одновременно.
    Раньше каждый блокировал ответы другого и ждал их
    на каскаде (40P01); теперь второй ждёт общую пару, а первый не трогает строку второго в
    users (сообщения партнёра меняются одной командой). Держатель строк Y и Z задаёт порядок."""
    anna, igor, couple, x, y, z = await _cross(sessionmaker, engine)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE partner_messages SET status = 'delivered' WHERE id = :z"), {"z": z}
        )
    if dissolved:
        await _call(sessionmaker, igor.user_id, "SELECT app_leave_couple()")
    a = ("app_delete_account", _try(sessionmaker, anna.user_id, DELETE))
    i = ("app_delete_account", _try(sessionmaker, igor.user_id, DELETE))
    steps = [a, i] if first == "anna" else [i, a]
    async with engine.connect() as holder:
        await holder.execute(
            text("SELECT 1 FROM partner_messages WHERE id = ANY(:m) FOR UPDATE"), {"m": [y, z]}
        )
        tasks: list[asyncio.Future[object]] = []
        try:
            for n, (func, coro) in enumerate(steps, start=1):
                tasks.append(asyncio.ensure_future(coro))
                await _wait_lock_waiters(engine, func, n)
        finally:
            await holder.rollback()
        results = list(await asyncio.wait_for(asyncio.gather(*tasks), timeout=30))
    assert results == [None, None]
    assert await _traces(engine, anna) == {} and await _traces(engine, igor) == {}
    async with engine.connect() as conn:
        left = (
            await conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM couples WHERE id = :c), "
                    "(SELECT count(*) FROM partner_messages WHERE id = ANY(:m))"
                ),
                {"c": couple, "m": [x, y, z]},
            )
        ).one()
    assert tuple(left) == (0, 0)  # второй удалившийся убирает опустевшую пару с перепиской


async def test_pipeline_after_recipient_deleted_keeps_no_recipient_version(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Сообщение Игоря Анне ещё в конвейере, Анна удаляет аккаунт: доставка проходит, но
    версии для получателя (адаптация, тон, предупреждение) нет — её некому показывать (АР-23,
    ревью 4.5 MINOR-3). Остановка безопасностью (withhold) по-прежнему работает."""
    anna, igor = await _person(engine, "Анна"), await _person(engine, "Игорь")
    couple, _ = await _couple(sessionmaker, anna, igor)
    p1 = await _message(engine, couple, igor, anna, b"p1 " + igor.marker)
    p2 = await _message(engine, couple, igor, anna, b"p2 " + igor.marker)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE partner_messages SET status = 'processing', adapted_enc = NULL "
                "WHERE id IN (:a, :b)"
            ),
            {"a": p1, "b": p2},
        )
    await _call(sessionmaker, anna.user_id, DELETE)
    assert await _worker_try(engine, DELIVER, m=p1) is True
    assert await _worker_try(engine, WITHHOLD, m=p2) is True
    row = await _row(
        engine,
        "SELECT recipient_id, status::text AS status, adapted_enc, is_adapted, tone_score, "
        "harsh_tone_warning, original_enc FROM partner_messages WHERE id = :m",
        m=p1,
    )
    assert row == {
        "recipient_id": None,
        "status": "delivered",
        "adapted_enc": None,
        "is_adapted": False,
        "tone_score": None,
        "harsh_tone_warning": False,
        "original_enc": b"p1 " + igor.marker,
    }
    assert (
        await _row(engine, "SELECT status::text AS s FROM partner_messages WHERE id = :m", m=p2)
    )["s"] == "withheld"
    # Получатель есть — версия для получателя сохраняется как раньше (контракт 3.2).
    olga = await _person(engine, "Ольга")
    c2, _ = await _couple(sessionmaker, igor, olga)
    p3 = await _message(engine, c2, igor, olga, b"p3")
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE partner_messages SET status = 'processing' WHERE id = :m"), {"m": p3}
        )
    assert await _worker_try(engine, DELIVER, m=p3) is True
    row = await _row(
        engine,
        "SELECT adapted_enc, is_adapted, tone_score, harsh_tone_warning FROM partner_messages "
        "WHERE id = :m",
        m=p3,
    )
    assert row == {
        "adapted_enc": b"\x01",
        "is_adapted": True,
        "tone_score": 2,
        "harsh_tone_warning": True,
    }


# --- app_send_draft: коды 0005 (ревью 4.5 MINOR-4) ----------------------------------------


async def test_send_draft_42501_without_login_or_for_deleted_sender(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Без app.user_id и для отправителя с deleted_at — 42501 (до 0005 — FA001); черновик
    не отправлен, сообщения нет."""
    anna, igor = await _person(engine, "Анна"), await _person(engine, "Игорь")
    await _couple(sessionmaker, anna, igor)
    await _fails(sessionmaker, None, "42501", SEND, d=anna.draft_id)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": anna.user_id}
        )
    await _fails(sessionmaker, anna.user_id, "42501", SEND, d=anna.draft_id)
    row = await _row(
        engine,
        "SELECT d.status::text AS status, (SELECT count(*) FROM partner_messages m "
        "WHERE m.draft_id = d.id) AS n FROM message_drafts d WHERE d.id = :d",
        d=anna.draft_id,
    )
    assert row == {"status": "draft", "n": 0}


async def test_send_draft_recipient_changed_while_waiting_gets_fa003(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Ветка ``v_recipient IS DISTINCT FROM v_locked``: Вера с ожидающей парой жмёт «Отправить»
    (получателя нет — строка не блокирована), пока отправка ждёт черновик, Гость принимает
    ссылку. После ожидания пара активна, но получатель не тот, чья строка заблокирована, —
    FA003, сообщения нет; повторная отправка уходит новому партнёру."""
    vera = await _person(engine, "Вера")
    await _call(sessionmaker, vera.user_id, CREATE)
    token = _token()
    await _call(sessionmaker, vera.user_id, "SELECT app_issue_invite(:h)", h=token)
    guest = await _person(engine, "Гость")
    async with engine.connect() as holder:
        await holder.execute(
            text("SELECT 1 FROM message_drafts WHERE id = :d FOR UPDATE"), {"d": vera.draft_id}
        )
        task = asyncio.ensure_future(_try(sessionmaker, vera.user_id, SEND, d=vera.draft_id))
        try:
            await _wait_lock_waiters(engine, "app_send_draft", 1)
            assert await _try(sessionmaker, guest.user_id, ACCEPT, h=token) is not None
        finally:
            await holder.rollback()
        assert await asyncio.wait_for(task, timeout=30) == "FA003"
    assert (
        await _row(
            engine,
            "SELECT count(*) AS n FROM partner_messages WHERE author_id = :u",
            u=vera.user_id,
        )
    )["n"] == 0
    msg = await _call(sessionmaker, vera.user_id, SEND, d=vera.draft_id)
    row = await _row(engine, "SELECT recipient_id FROM partner_messages WHERE id = :m", m=msg)
    assert row == {"recipient_id": guest.user_id}


# --- Журнал: старая сессия не возвращает id удалённого (ревью 4.5-test M-1) ---------------


AUDIT = (
    "INSERT INTO audit_log (actor_id, action, object_type) "
    "VALUES (app_uid(), 'me.export', 'user') RETURNING 1"
)


async def test_audit_insert_only_for_existing_user(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Живой пользователь пишет аудит со своим actor_id; после удаления запрос с прежним
    app.user_id получает отказ RLS (42501) и не возвращает id в журнал; чужой actor_id — тоже
    отказ; помеченный deleted_at — отказ."""
    anna, igor = await _person(engine, "Анна"), await _person(engine, "Игорь")
    assert isinstance(await _call(sessionmaker, igor.user_id, AUDIT), int)
    await _fails(
        sessionmaker,
        igor.user_id,
        "42501",
        "INSERT INTO audit_log (actor_id, action, object_type) "
        "VALUES (:a, 'me.export', 'user') RETURNING 1",
        a=anna.user_id,
    )
    await _call(sessionmaker, anna.user_id, DELETE)
    await _fails(sessionmaker, anna.user_id, "42501", AUDIT)
    assert await _traces(engine, anna) == {}
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": igor.user_id}
        )
    await _fails(sessionmaker, igor.user_id, "42501", AUDIT)


async def test_audit_insert_waits_for_running_delete(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Запись аудита во время незавершённого удаления ждёт его (FOR KEY SHARE строки users в
    политике) и после него отклоняется — id не попадает в журнал мимо обезличивания."""
    anna = await _person(engine, "Анна")
    async with engine.connect() as holder:
        # Держатель — строка журнала с её id: удаление встаёт на обезличивании, уже
        # заблокировав строку users.
        await holder.execute(
            text(
                "INSERT INTO audit_log (actor_id, action, object_type) "
                "VALUES (:u, 'user.update', 'user')"
            ),
            {"u": anna.user_id},
        )
        await holder.commit()
        await holder.execute(
            text("SELECT 1 FROM audit_log WHERE actor_id = :u FOR UPDATE"), {"u": anna.user_id}
        )
        steps = [
            ("app_delete_account", _try(sessionmaker, anna.user_id, DELETE)),
            ("INSERT INTO audit_log ", _try(sessionmaker, anna.user_id, AUDIT)),
        ]
        tasks: list[asyncio.Future[object]] = []
        try:
            for func, coro in steps:
                tasks.append(asyncio.ensure_future(coro))
                await _wait_lock_waiters(engine, func, 1)
        finally:
            await holder.rollback()
        deleted, audited = await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)
    assert (deleted, audited) == (None, "42501")
    assert await _traces(engine, anna) == {}


# --- Миграция обратима ---------------------------------------------------------------------


async def test_migration_0005_reversible(engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    """0005 → 0004 → 0005: downgrade возвращает DELETE на users и agents, политики users_self,
    agents_owner, drafts_delete и audit_insert из 0001, app_send_draft, pipeline_deliver и
    pipeline_withhold из 0001 и убирает app_delete_account; повторный upgrade снова их меняет."""

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
                    "pg_get_functiondef('app_send_draft(uuid, integer)'::regprocedure)) > 0, "
                    "position('FOR KEY SHARE' IN pg_get_functiondef("
                    "'pipeline_deliver(uuid, bytea, boolean, smallint, boolean)'::regprocedure)) > 0, "
                    "position('FOR KEY SHARE' IN "
                    "pg_get_functiondef('pipeline_withhold(uuid)'::regprocedure)) > 0, "
                    "(SELECT position('users' IN with_check) > 0 FROM pg_policies "
                    " WHERE policyname = 'audit_insert')"
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
                # 0009 (Б-63, АР-36): INSERT/UPDATE агента — по колонкам, без служебных memory_*.
                "SELECT has_table_privilege('feelings_app', 'agents', 'DELETE'), "
                "has_any_column_privilege('feelings_app', 'agents', 'INSERT'), "
                "has_any_column_privilege('feelings_app', 'agents', 'UPDATE'), "
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

"""0.7, A8: дополнительные негативные тесты изоляции (ТЗ §6.3, §7 «Правила доступа», §9.1, §9.2).

Сценарий: Анна (A) и Игорь (B) — активная пара с подпиской и платежом; Ольга (C) —
посторонняя со своей ожидающей парой. Данные заводит суперпользователь (в обход RLS),
проверки — через `user_scope` (роль feelings_app + app.user_id), как в приложении.

Раунд 2 (АР-10…АР-12): приложение входит как feelings_api и в user_scope становится
feelings_app; сообщения пары, пары, подписки меняются только функциями app_*/pipeline_*, права
на таблицы — по колонкам. Проверяемые свойства раунда 1 те же; там, где раньше защита давала
«0 строк», теперь ожидается «permission denied», а итог проверяется суперпользователем.

Тесты, помеченные xfail(strict=True), фиксируют дефекты продукта из docs/reviews/0.7-test.md:
когда дефект исправят, тест станет XPASS и упадёт — пометку нужно снять.
"""

import asyncio
import hashlib
import itertools
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db import user_scope
from tests.conftest import TEST_DATABASE_URL

_tg = itertools.count(5_000_000)

RLS_DENIED = "row-level security"
DENIED = "permission denied"


@dataclass
class P:
    user_id: uuid.UUID
    agent_id: uuid.UUID
    conversation_id: uuid.UUID
    conv_message_id: uuid.UUID
    memory_id: uuid.UUID
    draft_id: uuid.UUID


@dataclass
class W:
    a: P
    b: P
    c: P
    couple_id: uuid.UUID
    c_couple_id: uuid.UUID
    message_id: uuid.UUID
    subscription_id: uuid.UUID
    payment_id: uuid.UUID
    invite_token_hash: bytes


async def _person(s: AsyncSession, name: str) -> P:
    p = P(*(uuid.uuid4() for _ in range(6)))
    tg = next(_tg)
    # Онбординг пройден (согласия и 18+, ТЗ §4.1 п.2): иначе пара — FB008 (миграция 0003).
    await s.execute(
        text(
            "INSERT INTO users (id, telegram_id, name, consent_terms_at, consent_data_at, "
            "consent_sensitive_at, age_confirmed_at) "
            "VALUES (:i, :t, :n, now(), now(), now(), now())"
        ),
        {"i": p.user_id, "t": tg, "n": name},
    )
    await s.execute(
        text("INSERT INTO agents (id, user_id) VALUES (:a, :u)"),
        {"a": p.agent_id, "u": p.user_id},
    )
    await s.execute(
        text("INSERT INTO conversations (id, agent_id) VALUES (:c, :a)"),
        {"c": p.conversation_id, "a": p.agent_id},
    )
    await s.execute(
        text(
            "INSERT INTO conversation_messages (id, conversation_id, author, text_enc) "
            "VALUES (:i, :c, 'owner', :t)"
        ),
        {"i": p.conv_message_id, "c": p.conversation_id, "t": f"исповедь {name}".encode()},
    )
    await s.execute(
        text(
            "INSERT INTO memory_items (fingerprint, id, agent_id, type, text_enc, source) "
            "VALUES (sha256(convert_to(CAST(gen_random_uuid() AS text), 'UTF8')), "  # Б-68 (A1): отпечаток
            ":i, :a, 'fact', :t, 'owner_dialog')"
        ),
        {"i": p.memory_id, "a": p.agent_id, "t": f"память {name}".encode()},
    )
    await s.execute(
        text("INSERT INTO message_drafts (id, agent_id, text_enc) VALUES (:i, :a, :t)"),
        {"i": p.draft_id, "a": p.agent_id, "t": f"черновик {name}".encode()},
    )
    return p


@pytest.fixture
async def w(engine: AsyncEngine) -> W:
    sm = async_sessionmaker(engine)
    async with sm() as s, s.begin():
        a, b, c = await _person(s, "Анна"), await _person(s, "Игорь"), await _person(s, "Ольга")
        couple, c_couple, msg, sub, pay = (uuid.uuid4() for _ in range(5))
        await s.execute(
            text("INSERT INTO couples (id, status) VALUES (:k, 'active'), (:o, 'pending')"),
            {"k": couple, "o": c_couple},
        )
        await s.execute(
            text(
                "INSERT INTO couple_members (couple_id, user_id, role) VALUES "
                "(:k, :a, 'initiator'), (:k, :b, 'partner'), (:o, :c, 'initiator')"
            ),
            {"k": couple, "o": c_couple, "a": a.user_id, "b": b.user_id, "c": c.user_id},
        )
        await s.execute(
            text(
                "INSERT INTO partner_messages (id, couple_id, author_id, recipient_id, "
                "original_enc, status) VALUES (:m, :k, :a, :b, :t, 'delivered')"
            ),
            {"m": msg, "k": couple, "a": a.user_id, "b": b.user_id, "t": b"original-A"},
        )
        await s.execute(
            text(
                "INSERT INTO subscriptions (id, couple_id, payer_id, plan, status, "
                "period_start, period_end) VALUES (:s, :k, :a, 'month', 'active', now(), "
                "now() + interval '30 days')"
            ),
            {"s": sub, "k": couple, "a": a.user_id},
        )
        await s.execute(
            text(
                "INSERT INTO payments (id, subscription_id, amount, provider, status) "
                "VALUES (:p, :s, 100, 'telegram_stars', 'succeeded')"
            ),
            {"p": pay, "s": sub},
        )
        token_hash = hashlib.sha256(uuid.uuid4().bytes).digest()
        await s.execute(
            text(
                "INSERT INTO invites (token_hash, couple_id, created_by, expires_at) "
                "VALUES (:h, :o, :c, :e)"
            ),
            {"h": token_hash, "o": c_couple, "c": c.user_id, "e": datetime.now(UTC) + timedelta(7)},
        )
    return W(a, b, c, couple, c_couple, msg, sub, pay, token_hash)


async def _count(s: AsyncSession, sql: str, **params: object) -> int:
    return int((await s.execute(text(sql), params)).scalar_one())


async def _leave(sm: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> None:
    """Выход только функцией (Б-13, миграция 0004): прямых UPDATE пары у приложения нет."""
    async with user_scope(sm, uid) as s:
        await s.execute(text("SELECT app_leave_couple()"))


async def _su_scalar(engine: AsyncEngine, sql: str, **params: object) -> Any:
    """Проверка итога суперпользователем (в обход RLS)."""
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


# --- Личные данные по прямому id: ТЗ §7 «Правила доступа», §2 п.4, §6.3, §9.2 п.1 ---------

DIRECT = {
    "conversation_messages": ("conversation_messages", "conv_message_id"),
    "memory_items": ("memory_items", "memory_id"),
    "message_drafts": ("message_drafts", "draft_id"),
    "conversations": ("conversations", "conversation_id"),
    "agents": ("agents", "agent_id"),
    "users": ("users", "user_id"),
}


@pytest.mark.parametrize("key", sorted(DIRECT))
async def test_partner_and_outsider_cannot_read_by_direct_id(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], key: str
) -> None:
    table, attr = DIRECT[key]
    target = getattr(w.a, attr)
    for reader in (w.b, w.c):
        async with user_scope(sessionmaker, reader.user_id) as s:
            assert await _count(s, f"SELECT count(*) FROM {table} WHERE id = :i", i=target) == 0
    async with user_scope(sessionmaker, w.a.user_id) as s:
        assert await _count(s, f"SELECT count(*) FROM {table} WHERE id = :i", i=target) == 1


@pytest.mark.parametrize(
    ("table", "attr", "setter", "delete_granted"),
    [
        ("conversation_messages", "conv_message_id", "text_enc = 'x'", True),
        ("message_drafts", "draft_id", "text_enc = 'x', status = 'approved'", True),
        ("memory_items", "memory_id", "text_enc = 'x'", True),
        ("conversations", "conversation_id", "summary_enc = 'x'", True),
        # 4.5 (миграция 0005, Б-38): DELETE на agents у приложения отозван совсем —
        # агент удаляется только вместе с аккаунтом (app_delete_account).
        ("agents", "agent_id", "profile_summary_enc = 'x'", False),
    ],
)
async def test_partner_cannot_update_or_delete_private_rows(
    w: W,
    sessionmaker: async_sessionmaker[AsyncSession],
    table: str,
    attr: str,
    setter: str,
    delete_granted: bool,
) -> None:
    """Чужие реплики, черновики, память, агент — ни изменить, ни удалить (ТЗ §7, §6.3)."""
    target = getattr(w.a, attr)
    async with user_scope(sessionmaker, w.b.user_id) as s:
        upd = await s.execute(text(f"UPDATE {table} SET {setter} WHERE id = :i"), {"i": target})
        assert upd.rowcount == 0  # type: ignore[attr-defined]
    if delete_granted:
        async with user_scope(sessionmaker, w.b.user_id) as s:
            dele = await s.execute(text(f"DELETE FROM {table} WHERE id = :i"), {"i": target})
            assert dele.rowcount == 0  # type: ignore[attr-defined]
    else:
        with pytest.raises(ProgrammingError, match=DENIED):
            async with user_scope(sessionmaker, w.b.user_id) as s:
                await s.execute(text(f"DELETE FROM {table} WHERE id = :i"), {"i": target})
    async with user_scope(sessionmaker, w.a.user_id) as s:
        assert await _count(s, f"SELECT count(*) FROM {table} WHERE id = :i", i=target) == 1


async def test_cannot_move_own_rows_under_partner_agent(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Подложить свою запись в чужую память / чужой чат переносом agent_id нельзя (ТЗ §6.3)."""
    for sql, i in (
        ("UPDATE memory_items SET agent_id = :t WHERE id = :i", w.b.memory_id),
        ("UPDATE message_drafts SET agent_id = :t WHERE id = :i", w.b.draft_id),
        ("UPDATE conversations SET agent_id = :t WHERE id = :i", w.b.conversation_id),
    ):
        with pytest.raises(ProgrammingError, match=RLS_DENIED):
            async with user_scope(sessionmaker, w.b.user_id) as s:
                await s.execute(text(sql), {"t": w.a.agent_id, "i": i})


async def test_cannot_insert_into_partner_conversation_or_drafts(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    for sql, params in (
        (
            "INSERT INTO conversation_messages (conversation_id, author, text_enc) "
            "VALUES (:c, 'agent', 'x')",
            {"c": w.a.conversation_id},
        ),
        ("INSERT INTO message_drafts (agent_id, text_enc) VALUES (:a, 'x')", {"a": w.a.agent_id}),
        ("INSERT INTO agents (user_id) VALUES (:u)", {"u": w.a.user_id}),
    ):
        with pytest.raises(ProgrammingError, match=RLS_DENIED):
            async with user_scope(sessionmaker, w.b.user_id) as s:
                await s.execute(text(sql), params)


# --- Подписки и платежи пары: ФТ-19, ТЗ §7, АР-12 -------------------------------------------


async def test_outsider_cannot_read_or_touch_couple_subscription_and_payments(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    async with user_scope(sessionmaker, w.c.user_id) as s:
        assert await _count(s, "SELECT count(*) FROM subscriptions") == 0
        assert await _count(s, "SELECT count(*) FROM payments") == 0
    for who in (w.c, w.a):  # ни посторонний, ни участник пары не пишут в подписку напрямую
        with pytest.raises(ProgrammingError, match=DENIED):
            async with user_scope(sessionmaker, who.user_id) as s:
                await s.execute(
                    text("UPDATE subscriptions SET status = 'canceled' WHERE id = :i"),
                    {"i": w.subscription_id},
                )
        with pytest.raises(ProgrammingError, match=DENIED):
            async with user_scope(sessionmaker, who.user_id) as s:
                await s.execute(
                    text(
                        "INSERT INTO payments (subscription_id, amount, provider, status) "
                        "VALUES (:s, 1, 'x', 'succeeded')"
                    ),
                    {"s": w.subscription_id},
                )
    for member in (w.a, w.b):  # статус оплаты виден обоим партнёрам (ФТ-19)
        async with user_scope(sessionmaker, member.user_id) as s:
            assert await _count(s, "SELECT count(*) FROM payments") == 1
    async with engine.connect() as conn:
        st = await conn.execute(
            text("SELECT status FROM subscriptions WHERE id = :i"), {"i": w.subscription_id}
        )
        assert st.scalar_one() == "active"
        n = await conn.execute(
            text("SELECT count(*) FROM payments WHERE subscription_id = :i"),
            {"i": w.subscription_id},
        )
        assert n.scalar_one() == 1


# --- partner_messages: ФТ-08, ФТ-10, ФТ-11, ТЗ §2 п.2, §4.3 п.3, §6.2, §7, АР-11 --------------


async def _message_row(engine: AsyncEngine, msg: uuid.UUID) -> tuple[Any, ...]:
    async with engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT couple_id, author_id, recipient_id, original_enc, adapted_enc, status "
                "FROM partner_messages WHERE id = :m"
            ),
            {"m": msg},
        )
        return tuple(row.one())


async def _set_user(s: AsyncSession, uid: uuid.UUID) -> None:
    await s.execute(text("SET LOCAL ROLE feelings_app"))
    await s.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(uid)})


async def _worker(engine: AsyncEngine, sql: str, **params: object) -> Any:
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL ROLE feelings_worker"))
        return (await conn.execute(text(sql), params)).scalar_one()


async def _new_draft(sm: async_sessionmaker[AsyncSession], p: P, body: bytes) -> uuid.UUID:
    async with user_scope(sm, p.user_id) as s:
        row = await s.execute(
            text("INSERT INTO message_drafts (agent_id, text_enc) VALUES (:a, :t) RETURNING id"),
            {"a": p.agent_id, "t": body},
        )
        return uuid.UUID(str(row.scalar_one()))


async def _send(sm: async_sessionmaker[AsyncSession], p: P, draft: uuid.UUID) -> uuid.UUID:
    async with user_scope(sm, p.user_id) as s:
        value = (await s.execute(text("SELECT app_send_draft(:d, 1)"), {"d": draft})).scalar_one()
        return uuid.UUID(str(value))


async def _feed_ids(sm: async_sessionmaker[AsyncSession], uid: uuid.UUID | None) -> set[uuid.UUID]:
    async with user_scope(sm, uid) as s:
        return {r.id for r in await s.execute(text("SELECT id FROM app_couple_feed(100, NULL)"))}


async def test_partner_message_cannot_be_deleted_by_participants(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    before = await _message_row(engine, w.message_id)
    for p in (w.a, w.b, w.c):
        with pytest.raises(ProgrammingError, match=DENIED):
            async with user_scope(sessionmaker, p.user_id) as s:
                await s.execute(
                    text("DELETE FROM partner_messages WHERE id = :m"), {"m": w.message_id}
                )
    assert await _message_row(engine, w.message_id) == before


async def test_outsider_cannot_update_or_insert_partner_message(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    before = await _message_row(engine, w.message_id)
    with pytest.raises(ProgrammingError, match=DENIED):
        async with user_scope(sessionmaker, w.c.user_id) as s:
            await s.execute(
                text("UPDATE partner_messages SET recipient_id = :c WHERE id = :m"),
                {"c": w.c.user_id, "m": w.message_id},
            )
    with pytest.raises(ProgrammingError, match=DENIED):
        async with user_scope(sessionmaker, w.c.user_id) as s:
            await s.execute(
                text(
                    "INSERT INTO partner_messages (couple_id, author_id, recipient_id, "
                    "original_enc) VALUES (:k, :c, :a, 'spam')"
                ),
                {"k": w.couple_id, "c": w.c.user_id, "a": w.a.user_id},
            )
    assert await _message_row(engine, w.message_id) == before


async def test_recipient_can_mark_read_and_view_original_only_via_functions(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """Получатель отмечает прочтение и просмотр оригинала (ФТ-10, ФТ-11) — только функциями."""
    with pytest.raises(ProgrammingError, match=DENIED):
        async with user_scope(sessionmaker, w.b.user_id) as s:
            await s.execute(
                text("UPDATE partner_messages SET status = 'read' WHERE id = :m"),
                {"m": w.message_id},
            )
    async with user_scope(sessionmaker, w.b.user_id) as s:
        assert (await s.execute(text("SELECT app_mark_read(:m)"), {"m": w.message_id})).scalar()
        orig = (
            await s.execute(text("SELECT app_read_original(:m)"), {"m": w.message_id})
        ).scalar_one()
        assert bytes(orig) == b"original-A"
        # повторная отметка — уже не delivered
        assert (
            await s.execute(text("SELECT app_mark_read(:m)"), {"m": w.message_id})
        ).scalar_one() is False
    async with engine.connect() as conn:
        row = await conn.execute(
            text("SELECT status, read_at, original_viewed_at FROM partner_messages WHERE id = :m"),
            {"m": w.message_id},
        )
        status, read_at, viewed = row.one()
        assert status == "read"
        assert read_at is not None
        assert viewed is not None


async def test_recipient_cannot_forge_author(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """D-1: получатель не может подменить автора сообщения (ТЗ §2 п.2, ФТ-08)."""
    with pytest.raises(ProgrammingError, match=DENIED):
        async with user_scope(sessionmaker, w.b.user_id) as s:
            await s.execute(
                text("UPDATE partner_messages SET author_id = :c WHERE id = :m"),
                {"c": w.c.user_id, "m": w.message_id},
            )
    assert (await _message_row(engine, w.message_id))[1] == w.a.user_id


async def test_original_is_immutable(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """D-1: оригинал, одобренный автором, не меняется ни получателем, ни автором
    (ТЗ §4.3 п.3, ФТ-10); адаптацию тоже пишет только конвейер."""
    for p in (w.b, w.a):
        for setter in ("original_enc = 'forged'", "adapted_enc = 'forged', is_adapted = true"):
            with pytest.raises(ProgrammingError, match=DENIED):
                async with user_scope(sessionmaker, p.user_id) as s:
                    await s.execute(
                        text(f"UPDATE partner_messages SET {setter} WHERE id = :m"),
                        {"m": w.message_id},
                    )
    row = await _message_row(engine, w.message_id)
    assert bytes(row[3]) == b"original-A"
    assert row[4] is None
    async with user_scope(sessionmaker, w.a.user_id) as s:
        own = (
            await s.execute(text("SELECT app_read_original(:m)"), {"m": w.message_id})
        ).scalar_one()
        assert bytes(own) == b"original-A"


async def test_author_cannot_redirect_message_to_outsider(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """D-1: сообщения пары читают только двое участников (ТЗ §7): автор не может
    «переадресовать» отправленное постороннему или перенести его в чужую пару."""
    with pytest.raises(ProgrammingError, match=DENIED):
        async with user_scope(sessionmaker, w.a.user_id) as s:
            await s.execute(
                text("UPDATE partner_messages SET recipient_id = :c, couple_id = :o WHERE id = :m"),
                {"c": w.c.user_id, "o": w.c_couple_id, "m": w.message_id},
            )
    row = await _message_row(engine, w.message_id)
    assert (row[0], row[2]) == (w.couple_id, w.b.user_id)
    async with user_scope(sessionmaker, w.c.user_id) as s:
        assert await _count(s, "SELECT count(*) FROM partner_messages") == 0
        assert (
            await s.execute(text("SELECT app_read_original(:m)"), {"m": w.message_id})
        ).scalar_one() is None
    assert await _feed_ids(sessionmaker, w.c.user_id) == set()


async def test_concurrent_send_of_same_draft_creates_one_message(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """Гонка двух «Отправить» одного черновика (двойной клик, два устройства): ровно одно
    сообщение, второй вызов ждёт блокировку и получает «draft not sendable» (ФТ-08)."""
    call = text("SELECT app_send_draft(:d, 1)")
    async with sessionmaker() as s1, sessionmaker() as s2:
        await s1.begin()
        await s2.begin()
        await _set_user(s1, w.a.user_id)
        await _set_user(s2, w.a.user_id)
        first = (await s1.execute(call, {"d": w.a.draft_id})).scalar_one()
        second = asyncio.ensure_future(s2.execute(call, {"d": w.a.draft_id}))
        await asyncio.sleep(0.3)
        assert not second.done()  # ждёт блокировку строки черновика
        await s1.commit()
        with pytest.raises(DBAPIError, match="draft not sendable"):
            await second
        await s2.rollback()
    assert first is not None
    async with engine.connect() as conn:
        n = await conn.execute(
            text("SELECT count(*) FROM partner_messages WHERE draft_id = :d"), {"d": w.a.draft_id}
        )
        assert n.scalar_one() == 1


@pytest.mark.parametrize("status", ["withheld", "processing"])
async def test_cannot_reply_to_withheld_or_unprocessed_message(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine, status: str
) -> None:
    """Ответ на сообщение, которое получатель не видел (остановлено безопасностью или ещё в
    конвейере), невозможен и не меняет его статус (ТЗ §6.2, §9.3, ФТ-11)."""
    hidden = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO partner_messages (id, couple_id, author_id, recipient_id, "
                "original_enc, status) VALUES (:m, :k, :a, :b, 'hidden', :st)"
            ),
            {"m": hidden, "k": w.couple_id, "a": w.a.user_id, "b": w.b.user_id, "st": status},
        )
    with pytest.raises(DBAPIError, match="bad reply target"):
        async with user_scope(sessionmaker, w.b.user_id) as s:
            await s.execute(
                text("UPDATE message_drafts SET reply_to_id = :m WHERE id = :d"),
                {"m": hidden, "d": w.b.draft_id},
            )
            await s.execute(text("SELECT app_send_draft(:d, 1)"), {"d": w.b.draft_id})
    assert (await _message_row(engine, hidden))[5] == status
    async with engine.connect() as conn:
        n = await conn.execute(
            text("SELECT count(*) FROM partner_messages WHERE author_id = :b"), {"b": w.b.user_id}
        )
        assert n.scalar_one() == 0


async def test_cannot_reply_to_message_of_another_couple(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """Ответ «привязывается» только к входящему своей активной пары."""
    foreign = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO partner_messages (id, couple_id, author_id, recipient_id, "
                "original_enc, status) VALUES (:m, :o, :c, :b, 'x', 'delivered')"
            ),
            {"m": foreign, "o": w.c_couple_id, "c": w.c.user_id, "b": w.b.user_id},
        )
    with pytest.raises(DBAPIError, match="bad reply target"):
        async with user_scope(sessionmaker, w.b.user_id) as s:
            await s.execute(
                text("UPDATE message_drafts SET reply_to_id = :m WHERE id = :d"),
                {"m": foreign, "d": w.b.draft_id},
            )
            await s.execute(text("SELECT app_send_draft(:d, 1)"), {"d": w.b.draft_id})
    assert (await _message_row(engine, foreign))[5] == "delivered"


async def test_feed_for_outsider_former_member_and_anonymous(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Лента: посторонний и аноним не видят ничего; бывший участник читает историю (Д7), но не
    отправляет; оставшийся партнёр тоже не отправляет в распавшуюся пару (ТЗ §4.5 п.2)."""
    assert await _feed_ids(sessionmaker, w.c.user_id) == set()
    assert await _feed_ids(sessionmaker, None) == set()
    await _leave(sessionmaker, w.a.user_id)
    assert w.message_id in await _feed_ids(sessionmaker, w.a.user_id)
    assert w.message_id in await _feed_ids(sessionmaker, w.b.user_id)
    for p in (w.a, w.b):
        with pytest.raises(DBAPIError, match="no active couple"):
            await _send(sessionmaker, p, p.draft_id)
    async with user_scope(sessionmaker, w.c.user_id) as s:
        assert (
            await s.execute(text("SELECT app_mark_read(:m)"), {"m": w.message_id})
        ).scalar_one() is False


async def test_pipeline_functions_are_single_shot(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """Конвейер (ТЗ §6.2 п.3–7) применяется к сообщению один раз: повторная доставка не
    подменяет версию для получателя, доставленное нельзя «остановить», остановленное —
    доставить; оригинал конвейеру отдаётся только до доставки."""
    deliver = "SELECT pipeline_deliver(:m, :t, true, 2::smallint, false)"
    msg = await _send(sessionmaker, w.a, w.a.draft_id)
    assert bytes(await _worker(engine, "SELECT pipeline_original(:m)", m=msg)) == (
        "черновик Анна".encode()
    )
    assert await _worker(engine, deliver, m=msg, t=b"first") is True
    assert await _worker(engine, deliver, m=msg, t=b"evil") is False
    assert await _worker(engine, "SELECT pipeline_withhold(:m)", m=msg) is False
    assert await _worker(engine, "SELECT pipeline_original(:m)", m=msg) is None
    row = await _message_row(engine, msg)
    assert (bytes(row[4]), row[5]) == (b"first", "delivered")

    stopped = await _send(sessionmaker, w.a, await _new_draft(sessionmaker, w.a, b"opasno"))
    assert await _worker(engine, "SELECT pipeline_withhold(:m)", m=stopped) is True
    assert await _worker(engine, deliver, m=stopped, t=b"late") is False
    assert (await _message_row(engine, stopped))[5] == "withheld"
    feed = await _feed_ids(sessionmaker, w.b.user_id)
    assert msg in feed
    assert stopped not in feed


async def test_worker_has_only_pipeline_functions(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """feelings_worker не читает таблицы напрямую и не вызывает функции приложения; роль входа
    приложения не может стать feelings_worker (АР-10, АР-11)."""
    for sql in (
        "SELECT original_enc FROM partner_messages",
        "SELECT count(*) FROM memory_items",
        "SELECT * FROM app_couple_feed(10, NULL)",
    ):
        with pytest.raises(ProgrammingError, match="permission denied"):
            async with engine.begin() as conn:
                await conn.execute(text("SET LOCAL ROLE feelings_worker"))
                await conn.execute(text(sql))
    for role in ("feelings_worker", "feelings_admin"):
        with pytest.raises(ProgrammingError, match="permission denied"):
            async with user_scope(sessionmaker, w.a.user_id) as s:
                await s.execute(text(f"SET LOCAL ROLE {role}"))


async def test_functions_without_login_reveal_nothing(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """До входа (app.user_id пуст) функции не отдают ни приглашения, ни ленты, ни оригинала,
    ни состава пары и ничего не создают (ФТ-01, ФТ-03, ТЗ §9.2)."""
    async with user_scope(sessionmaker, None) as s:
        for sql, params in (
            ("SELECT count(*) FROM app_find_invite(:h)", {"h": w.invite_token_hash}),
            ("SELECT count(*) FROM app_couple_feed(100, NULL)", {}),
            ("SELECT count(*) FROM app_couple_members(:k)", {"k": w.couple_id}),
        ):
            assert (await s.execute(text(sql), params)).scalar_one() == 0, sql
        for sql in ("SELECT app_read_original(:m)",):
            assert (await s.execute(text(sql), {"m": w.message_id})).scalar_one() is None
        assert (
            await s.execute(text("SELECT app_mark_read(:m)"), {"m": w.message_id})
        ).scalar_one() is False
        assert (
            await s.execute(text("SELECT app_is_member(:k)"), {"k": w.couple_id})
        ).scalar_one() is False
    for sql in ("SELECT app_create_couple()", "SELECT app_send_draft(:d, 1)"):
        with pytest.raises(ProgrammingError, match="no user"):
            async with user_scope(sessionmaker, None) as s:
                await s.execute(text(sql), {"d": w.a.draft_id})


async def test_find_invite_reveals_only_by_exact_token(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with user_scope(sessionmaker, w.a.user_id) as s:
        res = await s.execute(text("SELECT * FROM app_find_invite(:h)"), {"h": w.invite_token_hash})
        assert list(res.keys()) == ["couple_id", "inviter_name", "expires_at", "status"]
        rows = res.all()
        assert len(rows) == 1
        assert rows[0].inviter_name == "Ольга"
        for h in (None, b"", w.invite_token_hash[:-1], hashlib.sha256(b"guess").digest()):
            assert await _count(s, "SELECT count(*) FROM app_find_invite(:h)", h=h) == 0
        # Лишнего (хеш токена, принявший) не отдаёт, и сама invites чужой стороне не видна.
        assert await _count(s, "SELECT count(*) FROM invites") == 0


# --- Пары и приглашения: ФТ-03, ФТ-04, ТЗ §4.1, АР-12 ----------------------------------------


async def test_outsider_cannot_create_invite_into_foreign_couple(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """Б-13 (0004): INSERT invites у приложения отозван — только app_issue_invite."""
    with pytest.raises(ProgrammingError, match=DENIED):
        async with user_scope(sessionmaker, w.c.user_id) as s:
            await s.execute(
                text(
                    "INSERT INTO invites (token_hash, couple_id, created_by, expires_at) "
                    "VALUES ('\\x01', :k, :c, now() + interval '7 days')"
                ),
                {"k": w.couple_id, "c": w.c.user_id},
            )
    assert (
        await _su_scalar(engine, "SELECT count(*) FROM invites WHERE couple_id = :k", k=w.couple_id)
        == 0
    )


async def test_cannot_create_invite_in_someone_elses_name(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    with pytest.raises(ProgrammingError, match=DENIED):
        async with user_scope(sessionmaker, w.b.user_id) as s:
            await s.execute(
                text(
                    "INSERT INTO invites (token_hash, couple_id, created_by, expires_at) "
                    "VALUES ('\\x02', :k, :a, now() + interval '7 days')"
                ),
                {"k": w.couple_id, "a": w.a.user_id},
            )
    assert (
        await _su_scalar(engine, "SELECT count(*) FROM invites WHERE couple_id = :k", k=w.couple_id)
        == 0
    )


async def test_partner_cannot_see_or_revoke_invites_of_other(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """Чужие приглашения не видны; отозвать их прямым UPDATE нельзя никому, даже автору
    (Б-13, 0004: UPDATE invites отозван — только app_issue_invite/app_leave_couple)."""
    for p in (w.a, w.b):
        async with user_scope(sessionmaker, p.user_id) as s:
            assert await _count(s, "SELECT count(*) FROM invites") == 0
    for p in (w.a, w.b, w.c):
        with pytest.raises(ProgrammingError, match=DENIED):
            async with user_scope(sessionmaker, p.user_id) as s:
                await s.execute(text("UPDATE invites SET status = 'revoked'"))
    status = await _su_scalar(
        engine, "SELECT status::text FROM invites WHERE token_hash = :h", h=w.invite_token_hash
    )
    assert status == "active"


async def test_cannot_move_own_membership_into_foreign_couple(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """Вступить в чужую пару переносом своей записи couple_members нельзя (ФТ-03)."""
    sm = async_sessionmaker(engine)
    lonely = uuid.uuid4()
    lone_user = uuid.uuid4()
    async with sm() as s, s.begin():
        await s.execute(
            text("INSERT INTO users (id, telegram_id) VALUES (:u, :t)"),
            {"u": lone_user, "t": next(_tg)},
        )
        await s.execute(text("INSERT INTO couples (id) VALUES (:k)"), {"k": lonely})
        await s.execute(
            text(
                "INSERT INTO couple_members (couple_id, user_id, role) VALUES (:k, :u, 'initiator')"
            ),
            {"k": lonely, "u": lone_user},
        )
    for sql in (
        "UPDATE couple_members SET couple_id = :k, role = 'partner' WHERE user_id = :c",
        "INSERT INTO couple_members (couple_id, user_id, role) VALUES (:k, :c, 'partner')",
    ):
        with pytest.raises(ProgrammingError, match=DENIED):
            async with user_scope(sessionmaker, w.c.user_id) as s:
                await s.execute(text(sql), {"k": lonely, "c": w.c.user_id})
    async with engine.connect() as conn:
        n = await conn.execute(
            text("SELECT count(*) FROM couple_members WHERE couple_id = :k"), {"k": lonely}
        )
        assert n.scalar_one() == 1


async def test_couple_rows_cannot_be_deleted_by_member(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    for sql in (
        "DELETE FROM couples WHERE id = :k",
        "DELETE FROM couple_members WHERE couple_id = :k",
    ):
        with pytest.raises(ProgrammingError, match=DENIED):
            async with user_scope(sessionmaker, w.a.user_id) as s:
                await s.execute(text(sql), {"k": w.couple_id})
    async with engine.connect() as conn:
        n = await conn.execute(
            text("SELECT count(*) FROM couple_members WHERE couple_id = :k"), {"k": w.couple_id}
        )
        assert n.scalar_one() == 2


async def test_former_member_cannot_invite_into_or_dissolve_former_couple(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """D-4: после выхода обмен прекращается (ТЗ §4.5 п.2): бывший участник не приглашает в
    пару, не меняет её статус и не возвращается в неё (ни функцией, ни прямой записью)."""
    await _leave(sessionmaker, w.a.user_id)
    with pytest.raises(DBAPIError) as ei:
        async with user_scope(sessionmaker, w.a.user_id) as s:
            await s.execute(text("SELECT app_issue_invite(:h)"), {"h": b"\x05" * 32})
    assert getattr(ei.value.orig, "sqlstate", None) == "FB007"
    for sql in (
        "INSERT INTO invites (token_hash, couple_id, created_by, expires_at) "
        "VALUES ('\\x03', :k, :a, now() + interval '7 days')",
        "UPDATE couples SET status = 'active', dissolved_at = NULL WHERE id = :k",
        "UPDATE couple_members SET active = true, left_at = NULL WHERE user_id = :a",
    ):
        with pytest.raises(ProgrammingError, match=DENIED):
            async with user_scope(sessionmaker, w.a.user_id) as s:
                await s.execute(text(sql), {"k": w.couple_id, "a": w.a.user_id})
    assert (
        await _su_scalar(engine, "SELECT status::text FROM couples WHERE id = :k", k=w.couple_id)
        == "dissolved"
    )
    assert (
        await _su_scalar(
            engine,
            "SELECT count(*) FROM couple_members WHERE couple_id = :k AND active",
            k=w.couple_id,
        )
        == 0
    )
    assert (
        await _su_scalar(engine, "SELECT count(*) FROM invites WHERE couple_id = :k", k=w.couple_id)
        == 0
    )


async def test_create_couple_when_already_in_couple(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """Одна активная пара на пользователя (ТЗ §4.1 п.6, ФТ-04): app_create_couple при уже
    активной или ожидающей паре не создаёт второй; после выхода — можно."""

    async def couples_total() -> int:
        async with engine.connect() as conn:
            return int((await conn.execute(text("SELECT count(*) FROM couples"))).scalar_one())

    before = await couples_total()
    for p in (w.a, w.c):  # a — в активной паре, c — в ожидающей
        with pytest.raises(DBAPIError, match="uq_couple_members_one_active_per_user"):
            async with user_scope(sessionmaker, p.user_id) as s:
                await s.execute(text("SELECT app_create_couple()"))
    assert await couples_total() == before
    await _leave(sessionmaker, w.a.user_id)
    async with user_scope(sessionmaker, w.a.user_id) as s:
        new = (await s.execute(text("SELECT app_create_couple()"))).scalar_one()
        st = await s.execute(text("SELECT status FROM couples WHERE id = :k"), {"k": new})
        assert st.scalar_one() == "pending"
    assert await couples_total() == before + 1


async def test_invite_lifetime_is_limited_to_seven_days(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """ТЗ §4.1 п.6, ФТ-03: ссылка действует 7 дней. Ни продлить, ни создать «вечную» прямой
    записью нельзя (Б-13, 0004); срок ставит app_issue_invite — ровно 7 дней."""
    async with engine.begin() as conn:  # прежняя ссылка не мешает (индекс одной активной)
        await conn.execute(
            text("UPDATE invites SET status = 'revoked' WHERE couple_id = :k"), {"k": w.c_couple_id}
        )
    for sql in (
        "UPDATE invites SET expires_at = now() + interval '10 years'",
        "UPDATE invites SET status = 'active'",
    ):
        with pytest.raises(ProgrammingError, match=DENIED):
            async with user_scope(sessionmaker, w.c.user_id) as s:
                await s.execute(text(sql))
    async with user_scope(sessionmaker, w.c.user_id) as s:
        expires, now = (
            await s.execute(text("SELECT app_issue_invite(:h), now()"), {"h": b"\x06" * 32})
        ).one()
    assert expires - now == timedelta(days=7)
    with pytest.raises(ProgrammingError, match=DENIED):
        async with user_scope(sessionmaker, w.c.user_id) as s:
            await s.execute(
                text(
                    "INSERT INTO invites (token_hash, couple_id, created_by, expires_at) "
                    "VALUES ('\\x04', :k, :c, now() + interval '10 years')"
                ),
                {"k": w.c_couple_id, "c": w.c.user_id},
            )


async def test_blocked_draft_cannot_be_unblocked_and_sent(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """ТЗ §9.3, контракт POST /drafts/{id}/send (blocked_by_safety): черновик, остановленный
    проверкой безопасности, не отправляется. Статус blocked не должен сниматься клиентом."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE message_drafts SET status = 'blocked' WHERE id = :d"), {"d": w.a.draft_id}
        )
    with pytest.raises(DBAPIError):
        async with user_scope(sessionmaker, w.a.user_id) as s:
            await s.execute(
                text("UPDATE message_drafts SET status = 'approved' WHERE id = :d"),
                {"d": w.a.draft_id},
            )
            await s.execute(text("SELECT app_send_draft(:d, 1)"), {"d": w.a.draft_id})


# --- Функции SECURITY DEFINER: АР-7, АР-10, ТЗ §9.2 п.1 --------------------------------------

APP_FUNCS = [
    "app_uid()",
    "app_is_member(uuid)",
    "app_is_active_member(uuid)",
    "app_upsert_user(bigint)",
    "app_find_invite(bytea)",
    "app_couple_members(uuid)",
    "app_create_couple()",
    "app_send_draft(uuid, integer)",
    "app_couple_feed(integer, timestamp with time zone)",
    "app_read_original(uuid)",
    "app_mark_read(uuid)",
    "app_issue_invite(bytea)",  # 0002, задача 0.7b
    "app_accept_invite(bytea)",
    "app_leave_couple()",
    "app_delete_account()",  # 0005, задача 4.5
    "app_export_partner_messages()",  # 0006, задача 4.5 этап 2
    "app_export_safety_events()",  # 0007, задача 4.5 этап 2
    "app_memory_nearest(text, text, integer)",  # 0008, задача 2.0v
    "app_set_memory_embedding(uuid, text, text)",  # 0008
    "app_kb_nearest(text, text, integer, text, text[], integer)",  # 0008
    "app_delete_memory(uuid)",  # 0009, задача 2.4
    "app_memory_forgotten(bytea[])",  # 0009
    "app_memory_extraction_done(uuid, uuid, uuid)",  # 0009
    "app_memory_compaction_done(uuid)",  # 0009
]
PIPELINE_FUNCS = [
    "pipeline_original(uuid)",
    "pipeline_deliver(uuid, bytea, boolean, smallint, boolean)",
    "pipeline_withhold(uuid)",
    "kb_set_chunk_embedding(uuid, text, text)",  # 0008, задача 2.0v: воркер индексации
    "memory_claim_extraction(interval, interval, integer)",  # 0009, задача 2.4: воркер памяти
    "memory_claim_compaction(interval, interval, integer)",  # 0009
    "kb_claim_index(interval, integer)",  # 0011, задача 2.6: воркер индексации
    "kb_read_source(uuid, uuid)",  # 0011
    "kb_add_chunk(uuid, uuid, integer, text, text[], integer)",  # 0011
    "kb_finish_index(uuid, uuid)",  # 0011
    "kb_fail_index(uuid, uuid, text)",  # 0011
]
# 0011, задача 2.6: функции администратора (feelings_admin) — загрузка и удаление материала.
ADMIN_FUNCS = [
    "admin_kb_upload(text, text, text, text, text, bytea)",
    "admin_kb_delete(uuid)",
]


async def test_definer_functions_executable_only_by_intended_roles(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:

        async def can(role: str, f: str) -> bool:
            row = await conn.execute(
                text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"), {"r": role, "f": f}
            )
            return bool(row.scalar_one())

        for f in APP_FUNCS + PIPELINE_FUNCS:
            for role in ("feelings_admin", "public"):
                assert not await can(role, f), (role, f)
        for f in ADMIN_FUNCS:
            assert await can("feelings_admin", f), f
            for role in ("feelings_app", "feelings_api", "feelings_worker", "public"):
                assert not await can(role, f), (role, f)
        for f in APP_FUNCS:
            assert await can("feelings_app", f), f
            assert not await can("feelings_worker", f), f
            assert not await can("feelings_api", f), f  # NOINHERIT: только после SET ROLE
        for f in PIPELINE_FUNCS:
            assert await can("feelings_worker", f), f
            assert not await can("feelings_app", f), f
            assert not await can("feelings_api", f), f
        rows = await conn.execute(
            text(
                "SELECT p.proname, p.proconfig FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public' AND p.prosecdef"
            )
        )
        definers = rows.all()
        assert len(definers) == len(APP_FUNCS) + len(PIPELINE_FUNCS) + len(ADMIN_FUNCS) - 1
        for name, cfg in definers:
            paths = [c for c in (cfg or []) if c.startswith("search_path=")]
            assert paths, name
            assert paths[0].replace(" ", "").endswith(",pg_temp"), (name, paths[0])


async def test_no_temp_privilege_for_any_app_role(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """D-2, слой 1: временные таблицы запрещены — подменить ими таблицы нельзя."""
    async with engine.connect() as conn:
        for role in ("public", "feelings_app", "feelings_api", "feelings_worker", "feelings_admin"):
            row = await conn.execute(
                text("SELECT has_database_privilege(:r, current_database(), 'TEMPORARY')"),
                {"r": role},
            )
            assert row.scalar_one() is False, role
            row = await conn.execute(
                text("SELECT has_schema_privilege(:r, 'public', 'CREATE')"), {"r": role}
            )
            assert row.scalar_one() is False, role
    for ddl in (
        "CREATE TEMP TABLE couple_members (couple_id uuid, user_id uuid, active bool)",
        "CREATE TABLE public.evil (x int)",
    ):
        with pytest.raises(ProgrammingError, match="permission denied"):
            async with user_scope(sessionmaker, w.c.user_id) as s:
                await s.execute(text(ddl))


async def test_definer_functions_cannot_be_hijacked_by_temp_tables(
    w: W, engine: AsyncEngine
) -> None:
    """D-2, слой 2: даже если право TEMP вернут, временные couple_members/couples/
    partner_messages не подменяют настоящие внутри функций SECURITY DEFINER (search_path
    с pg_temp в конце, полные имена). Право выдаётся в откатываемой транзакции."""
    async with async_sessionmaker(engine)() as s:
        await s.begin()
        await s.execute(
            text(
                "DO $$ BEGIN EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO feelings_app', "
                "current_database()); END $$"
            )
        )
        await _set_user(s, w.c.user_id)
        await s.execute(
            text("CREATE TEMP TABLE couple_members (couple_id uuid, user_id uuid, active bool)")
        )
        await s.execute(text("CREATE TEMP TABLE couples (id uuid, status text)"))
        await s.execute(
            text(
                "CREATE TEMP TABLE partner_messages (id uuid, author_id uuid, recipient_id uuid, "
                "original_enc bytea, adapted_enc bytea, status text)"
            )
        )
        await s.execute(
            text("INSERT INTO pg_temp.couple_members VALUES (:k, :c, true), (:k, :a, true)"),
            {"k": w.couple_id, "c": w.c.user_id, "a": w.a.user_id},
        )
        await s.execute(
            text("INSERT INTO pg_temp.couples VALUES (:k, 'active')"), {"k": w.couple_id}
        )
        await s.execute(
            text("INSERT INTO pg_temp.partner_messages VALUES (:m, :c, :c, 'x', 'x', 'delivered')"),
            {"m": w.message_id, "c": w.c.user_id},
        )
        seen = await _count(s, "SELECT count(*) FROM public.couples WHERE id = :k", k=w.couple_id)
        subs = await _count(s, "SELECT count(*) FROM public.subscriptions")
        member = (
            await s.execute(text("SELECT app_is_member(:k)"), {"k": w.couple_id})
        ).scalar_one()
        names = await _count(s, "SELECT count(*) FROM app_couple_members(:k)", k=w.couple_id)
        orig = (
            await s.execute(text("SELECT app_read_original(:m)"), {"m": w.message_id})
        ).scalar_one()
        feed = await _count(s, "SELECT count(*) FROM app_couple_feed(100, NULL)")
        await s.execute(text("SAVEPOINT send"))
        with pytest.raises(DBAPIError, match="no active couple"):
            await s.execute(text("SELECT app_send_draft(:d, 1)"), {"d": w.c.draft_id})
        await s.rollback()
    assert (seen, subs, member, names, orig, feed) == (0, 0, False, 0, None, 0)
    async with engine.connect() as conn:
        row = await conn.execute(
            text("SELECT has_database_privilege('feelings_app', current_database(), 'TEMP')")
        )
        assert row.scalar_one() is False


async def test_membership_oracle_not_available_for_foreign_users(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """D-6: посторонний не узнаёт, кто состоит в чужой паре (ТЗ §7, §9.2 п.2): функции
    проверки членства не принимают чужого пользователя и отвечают только про себя."""
    with pytest.raises(ProgrammingError, match="does not exist"):
        async with user_scope(sessionmaker, w.c.user_id) as s:
            await s.execute(
                text("SELECT app_is_active_member(:k, :a)"), {"k": w.couple_id, "a": w.a.user_id}
            )
    with pytest.raises(ProgrammingError, match="does not exist"):
        async with user_scope(sessionmaker, w.c.user_id) as s:
            await s.execute(text("SELECT app_couple_has_members(:k)"), {"k": w.couple_id})
    async with user_scope(sessionmaker, w.c.user_id) as s:
        for f in ("app_is_member", "app_is_active_member"):
            v = (await s.execute(text(f"SELECT {f}(:k)"), {"k": w.couple_id})).scalar_one()
            assert v is False, f
        assert await _count(s, "SELECT count(*) FROM app_couple_members(:k)", k=w.couple_id) == 0
        assert await _count(s, "SELECT count(*) FROM couple_members") == 1  # только своя запись


# --- Роли: нельзя выйти из-под RLS (АР-7, АР-10, ТЗ §9.1 п.5, §9.2 п.4) ---------------------


async def test_app_role_cannot_disable_rls_or_alter_policies(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    for ddl in (
        "ALTER TABLE memory_items DISABLE ROW LEVEL SECURITY",
        "DROP POLICY memory_owner ON memory_items",
        "ALTER TABLE memory_items NO FORCE ROW LEVEL SECURITY",
        "GRANT UPDATE ON partner_messages TO feelings_app",
        "SET LOCAL row_security = off",
    ):
        with pytest.raises(DBAPIError):
            async with user_scope(sessionmaker, w.b.user_id) as s:
                await s.execute(text(ddl))
                # row_security=off под не-владельцем не отключает RLS, а даёт ошибку на запросе.
                await s.execute(text("SELECT count(*) FROM memory_items"))


async def test_roles_have_no_bypass_and_own_nothing(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        r = await conn.execute(
            text(
                "SELECT rolname, rolsuper, rolbypassrls, rolinherit, rolcreaterole, rolcreatedb "
                "FROM pg_roles WHERE rolname LIKE 'feelings\\_%' ORDER BY rolname"
            )
        )
        roles = {row[0]: tuple(row[1:]) for row in r}
        assert set(roles) >= {"feelings_app", "feelings_api", "feelings_worker", "feelings_admin"}
        for name, (sup, bypass, _inherit, createrole, createdb) in roles.items():
            assert (sup, bypass, createrole, createdb) == (False, False, False, False), name
        assert roles["feelings_api"][2] is False  # NOINHERIT
        for name in ("feelings_app", "feelings_worker", "feelings_admin"):
            lg = await conn.execute(
                text("SELECT rolcanlogin FROM pg_roles WHERE rolname = :r"), {"r": name}
            )
            assert lg.scalar_one() is False, name
        member_of = await conn.execute(
            text(
                "SELECT r.rolname, g.rolname FROM pg_auth_members m "
                "JOIN pg_roles r ON r.oid = m.member JOIN pg_roles g ON g.oid = m.roleid "
                "WHERE r.rolname IN ('feelings_api', 'feelings_app', 'feelings_worker', "
                "'feelings_admin')"
            )
        )
        assert sorted(tuple(x) for x in member_of) == [("feelings_api", "feelings_app")]
        owned = await conn.execute(
            text(
                "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
                "WHERE r.rolname LIKE 'feelings\\_%'"
            )
        )
        assert owned.scalar_one() == 0
        owned_f = await conn.execute(
            text(
                "SELECT count(*) FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner "
                "WHERE r.rolname LIKE 'feelings\\_%'"
            )
        )
        assert owned_f.scalar_one() == 0


async def test_dedicated_login_role_cannot_escape_to_superuser(engine: AsyncEngine) -> None:
    """Любая отдельная LOGIN-роль, которая входит только в feelings_app, не может стать
    суперпользователем. Роль своя, временная: общую feelings_api тест не трогает."""
    login = f"a8_login_{uuid.uuid4().hex[:8]}"
    async with engine.connect() as conn:
        await conn.execute(text(f"CREATE ROLE {login} LOGIN NOINHERIT IN ROLE feelings_app"))
        await conn.commit()
    parts = urlsplit(TEST_DATABASE_URL)
    host = parts.netloc.split("@")[-1]
    url = urlunsplit(parts._replace(netloc=f"{login}@{host}"))
    eng = create_async_engine(url)
    try:
        async with eng.connect() as conn:
            await conn.execute(text("SET ROLE feelings_app"))
            with pytest.raises(ProgrammingError, match="permission denied"):
                await conn.execute(text("SET ROLE postgres"))
    finally:
        await eng.dispose()
        async with engine.connect() as conn:
            await conn.execute(text(f"DROP ROLE {login}"))
            await conn.commit()
            alive = await conn.execute(
                text("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'feelings_api'")
            )
            assert alive.scalar_one() is True


@pytest.mark.parametrize(
    "escape",
    [
        "SET LOCAL ROLE postgres",
        "RESET ROLE",
        "SET LOCAL ROLE feelings_api",
        "SET SESSION AUTHORIZATION postgres",
    ],
)
async def test_user_scope_cannot_escape_role(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], escape: str
) -> None:
    """D-3: внутри user_scope нельзя стать суперпользователем, а вернувшись к роли входа
    (feelings_api, NOINHERIT) — прочитать хоть что-то, в том числе чужую память."""
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with user_scope(sessionmaker, w.b.user_id) as s:
            await s.execute(text(escape))
            await s.execute(text("SELECT count(*) FROM memory_items"))


async def test_app_engine_logs_in_as_api_role(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as conn:
        row = await conn.execute(text("SELECT session_user, current_user"))
        assert tuple(row.one()) == ("feelings_api", "feelings_api")


# --- Мутационная проверка: тесты действительно ловят отключённую защиту -------------------


async def test_mutation_disabled_rls_is_detected(w: W, engine: AsyncEngine) -> None:
    """В одной транзакции (откатывается) отключаем RLS на memory_items и проверяем, что та же
    проверка, что в тестах изоляции, видит чужую память. Значит, зелёные тесты изоляции —
    заслуга политик, а не пустых данных."""
    probe = "SELECT count(*) FROM memory_items WHERE id = :i"
    sm = async_sessionmaker(engine)
    async with sm() as s:
        await s.begin()
        await s.execute(text("ALTER TABLE memory_items DISABLE ROW LEVEL SECURITY"))
        await _set_user(s, w.b.user_id)
        leaked = await _count(s, probe, i=w.a.memory_id)
        await s.rollback()
    assert leaked == 1
    async with engine.connect() as conn:
        rls = await conn.execute(
            text("SELECT relrowsecurity FROM pg_class WHERE relname = 'memory_items'")
        )
        assert rls.scalar_one() is True


async def test_mutation_column_grant_is_detected(w: W, engine: AsyncEngine) -> None:
    """То же для колоночных прав: выдаём (с откатом) UPDATE (telegram_id) и SELECT
    (original_enc) — проверки D-5 и M5 перестают давать «permission denied»."""
    sm = async_sessionmaker(engine)
    async with sm() as s:
        await s.begin()
        await s.execute(text("GRANT UPDATE (telegram_id) ON users TO feelings_app"))
        await s.execute(text("GRANT SELECT (original_enc) ON partner_messages TO feelings_app"))
        await _set_user(s, w.a.user_id)
        upd = await s.execute(
            text("UPDATE users SET telegram_id = :t WHERE id = app_uid()"), {"t": next(_tg)}
        )
        changed = upd.rowcount  # type: ignore[attr-defined]
        orig = await _count(s, "SELECT count(original_enc) FROM partner_messages")
        await s.rollback()
    assert (changed, orig) == (1, 1)
    async with engine.connect() as conn:
        row = await conn.execute(
            text("SELECT has_column_privilege('feelings_app', 'users', 'telegram_id', 'UPDATE')")
        )
        assert row.scalar_one() is False


# --- Удаление аккаунта: ФТ-21, ТЗ §4.5 п.3, §7 ---------------------------------------------


async def test_account_deletion_cascades_personal_data(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    async with user_scope(sessionmaker, w.a.user_id) as s:
        await s.execute(
            text("INSERT INTO safety_events (user_id, type) VALUES (:u, 'self_harm')"),
            {"u": w.a.user_id},
        )
    # 4.5 (АР-23, Б-19): удаление — только функцией app_delete_account(); прямой DELETE
    # строки users у приложения отозван (проверка — test_cannot_delete_other_user).
    async with user_scope(sessionmaker, w.a.user_id) as s:
        await s.execute(text("SELECT app_delete_account()"))
    left = {
        "users": "SELECT count(*) FROM users WHERE id = :u",
        "agents": "SELECT count(*) FROM agents WHERE user_id = :u",
        "conversations": "SELECT count(*) FROM conversations WHERE agent_id = :a",
        "conversation_messages": "SELECT count(*) FROM conversation_messages WHERE id = :cm",
        "memory_items": "SELECT count(*) FROM memory_items WHERE agent_id = :a",
        "message_drafts": "SELECT count(*) FROM message_drafts WHERE agent_id = :a",
        "couple_members": "SELECT count(*) FROM couple_members WHERE user_id = :u",
        "partner_messages": (
            "SELECT count(*) FROM partner_messages WHERE author_id = :u OR recipient_id = :u"
        ),
        "safety_events": "SELECT count(*) FROM safety_events WHERE user_id = :u",
        "invites": "SELECT count(*) FROM invites WHERE created_by = :u OR used_by = :u",
        "subscriptions.payer": "SELECT count(*) FROM subscriptions WHERE payer_id = :u",
    }
    params = {"u": w.a.user_id, "a": w.a.agent_id, "cm": w.a.conv_message_id}
    async with engine.connect() as conn:
        for name, sql in left.items():
            n = (await conn.execute(text(sql), params)).scalar_one()
            assert n == 0, name
        # Данные партнёра (его агент и память) не пострадали (ТЗ §4.5 п.2–3).
        n = (
            await conn.execute(
                text("SELECT count(*) FROM memory_items WHERE agent_id = :b"), {"b": w.b.agent_id}
            )
        ).scalar_one()
        assert n == 1
        # Пара не осталась «активной с одним участником» (Б-19): распущена, Игорь свободен.
        row = await conn.execute(
            text(
                "SELECT c.status::text, bool_or(cm.active) FROM couples c "
                "JOIN couple_members cm ON cm.couple_id = c.id WHERE c.id = :k GROUP BY c.status"
            ),
            {"k": w.couple_id},
        )
        assert tuple(row.one()) == ("dissolved", False)


async def test_cannot_delete_other_user(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """Прямой DELETE на users отозван (Б-19, миграция 0005): ни чужую, ни свою строку
    приложение не удаляет — 42501; удаление аккаунта только функцией и только своего."""
    for actor, target in ((w.b, w.a), (w.a, w.a)):
        with pytest.raises(ProgrammingError, match=DENIED) as ei:
            async with user_scope(sessionmaker, actor.user_id) as s:
                await s.execute(text("DELETE FROM users WHERE id = :a"), {"a": target.user_id})
        assert getattr(ei.value.orig, "sqlstate", None) == "42501"
    # Функция удаляет только текущего пользователя: Игорь удалил себя — Анна на месте.
    async with user_scope(sessionmaker, w.b.user_id) as s:
        await s.execute(text("SELECT app_delete_account()"))
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT id FROM users WHERE id IN (:a, :b)"),
            {"a": w.a.user_id, "b": w.b.user_id},
        )
        assert [r.id for r in rows] == [w.a.user_id]


# --- Журналы без чтения: ТЗ §9.1 п.5, §9.3 --------------------------------------------------


async def test_audit_log_write_only_and_no_forged_actor(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with user_scope(sessionmaker, w.a.user_id) as s:
        await s.execute(
            text("INSERT INTO audit_log (actor_id, action, object_type) VALUES (:a, 'x', 'y')"),
            {"a": w.a.user_id},
        )
    for sql in ("SELECT count(*) FROM audit_log", "SELECT count(*) FROM safety_events"):
        with pytest.raises(ProgrammingError, match="permission denied"):
            async with user_scope(sessionmaker, w.a.user_id) as s:
                await s.execute(text(sql))
    with pytest.raises(ProgrammingError, match=RLS_DENIED):
        async with user_scope(sessionmaker, w.a.user_id) as s:
            await s.execute(
                text("INSERT INTO audit_log (actor_id, action, object_type) VALUES (:b, 'x', 'y')"),
                {"b": w.b.user_id},
            )
    with pytest.raises(ProgrammingError, match=RLS_DENIED):
        async with user_scope(sessionmaker, w.a.user_id) as s:
            await s.execute(
                text("INSERT INTO safety_events (user_id, type) VALUES (:b, 'self_harm')"),
                {"b": w.b.user_id},
            )


# --- Вход и учётная запись: ФТ-01, ФТ-21 ---------------------------------------------------


async def test_user_cannot_change_own_telegram_id(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """D-5: telegram_id — ключ входа (ФТ-01): пользователь не переписывает его на чужой
    (будущий) id, иначе тот при входе попадёт в чужую учётную запись."""
    for setter in ("telegram_id = :t", "id = gen_random_uuid()", "created_at = now()"):
        with pytest.raises(ProgrammingError, match="permission denied"):
            async with user_scope(sessionmaker, w.a.user_id) as s:
                await s.execute(
                    text(f"UPDATE users SET {setter} WHERE id = app_uid()"),
                    {"t": next(_tg)},
                )
    async with engine.connect() as conn:
        n = await conn.execute(text("SELECT count(*) FROM users WHERE id = :u"), {"u": w.a.user_id})
        assert n.scalar_one() == 1


async def test_relogin_after_account_delete_creates_new_user(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """D-7 / Б-3 (закрыт в 4.5, АР-23): после удаления аккаунта (ФТ-21) человек входит тем же
    Telegram ID и получает нового пользователя — без онбординга, агента, памяти и пары
    старого; app_upsert_user не возвращает NULL."""
    tg = next(_tg)
    async with user_scope(sessionmaker, None) as s:
        old = (await s.execute(text("SELECT app_upsert_user(:t)"), {"t": tg})).scalar_one()
    assert old is not None
    async with user_scope(sessionmaker, old) as s:
        await s.execute(
            text(
                "UPDATE users SET name = 'Старое имя', consent_terms_at = now(), "
                "consent_data_at = now(), consent_sensitive_at = now(), "
                "age_confirmed_at = now() WHERE id = app_uid()"
            )
        )
        agent = (
            await s.execute(text("INSERT INTO agents (user_id) VALUES (app_uid()) RETURNING id"))
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO memory_items (fingerprint, agent_id, type, text_enc, source) "
                "VALUES (sha256(convert_to(CAST(gen_random_uuid() AS text), 'UTF8')), "  # Б-68 (A1): отпечаток
                ":a, 'fact', 'old-memory', 'owner_dialog')"
            ),
            {"a": agent},
        )
        await s.execute(text("SELECT app_create_couple()"))
    async with user_scope(sessionmaker, old) as s:
        await s.execute(text("SELECT app_delete_account()"))
    async with user_scope(sessionmaker, None) as s:
        new = (await s.execute(text("SELECT app_upsert_user(:t)"), {"t": tg})).scalar_one()
    assert new is not None and new != old
    async with user_scope(sessionmaker, new) as s:
        me = (
            await s.execute(
                text("SELECT name, consent_terms_at, age_confirmed_at FROM users WHERE id = :u"),
                {"u": new},
            )
        ).one()
        assert tuple(me) == (None, None, None)
        for table in ("agents", "memory_items", "couples", "couple_members", "invites"):
            n = await _count(s, f"SELECT count(*) FROM {table}")
            assert n == 0, table
    async with engine.connect() as conn:
        left = await conn.execute(
            text("SELECT count(*) FROM users WHERE telegram_id = :t OR id = :o"),
            {"t": tg, "o": old},
        )
        assert left.scalar_one() == 1

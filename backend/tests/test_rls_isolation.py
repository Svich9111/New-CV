"""0.7: изоляция данных на уровне БД (ТЗ §6, §7 «Правила доступа», §9.2; риск Р6).

Сценарий: Анна (A) и Игорь (B) — активная пара; Ольга (C) — посторонний пользователь.
Данные заводятся суперпользователем (в обход RLS), проверки — через user_scope (роль
feelings_app + app.user_id), как в приложении.
"""

import hashlib
import itertools
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db import user_scope

_tg = itertools.count(1_000_000)


@dataclass
class Person:
    user_id: uuid.UUID
    agent_id: uuid.UUID
    conversation_id: uuid.UUID


@dataclass
class World:
    a: Person
    b: Person
    c: Person
    couple_id: uuid.UUID
    message_id: uuid.UUID
    invite_token_hash: bytes


async def _person(conn: AsyncSession, name: str) -> Person:
    uid, aid, cid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        text("INSERT INTO users (id, telegram_id, name) VALUES (:id, :tg, :n)"),
        {"id": uid, "tg": next(_tg), "n": name},
    )
    await conn.execute(
        text("INSERT INTO agents (id, user_id) VALUES (:a, :u)"), {"a": aid, "u": uid}
    )
    await conn.execute(
        text("INSERT INTO conversations (id, agent_id) VALUES (:c, :a)"), {"c": cid, "a": aid}
    )
    await conn.execute(
        text(
            "INSERT INTO conversation_messages (conversation_id, author, text_enc) "
            "VALUES (:c, 'owner', :t)"
        ),
        {"c": cid, "t": f"исповедь {name}".encode()},
    )
    await conn.execute(
        text(
            "INSERT INTO memory_items (agent_id, type, text_enc, source) "
            "VALUES (:a, 'fact', :t, 'owner_dialog')"
        ),
        {"a": aid, "t": f"память {name}".encode()},
    )
    await conn.execute(
        text("INSERT INTO message_drafts (agent_id, text_enc) VALUES (:a, :t)"),
        {"a": aid, "t": f"черновик {name}".encode()},
    )
    return Person(uid, aid, cid)


@pytest.fixture
async def world(engine: AsyncEngine) -> World:
    sm = async_sessionmaker(engine)
    async with sm() as s, s.begin():
        a, b, c = await _person(s, "Анна"), await _person(s, "Игорь"), await _person(s, "Ольга")
        couple = uuid.uuid4()
        await s.execute(
            text("INSERT INTO couples (id, status) VALUES (:c, 'active')"), {"c": couple}
        )
        await s.execute(
            text(
                "INSERT INTO couple_members (couple_id, user_id, role) VALUES "
                "(:c, :a, 'initiator'), (:c, :b, 'partner')"
            ),
            {"c": couple, "a": a.user_id, "b": b.user_id},
        )
        msg = uuid.uuid4()
        await s.execute(
            text(
                "INSERT INTO partner_messages (id, couple_id, author_id, recipient_id, "
                "original_enc, status) VALUES (:m, :c, :a, :b, :t, 'delivered')"
            ),
            {"m": msg, "c": couple, "a": a.user_id, "b": b.user_id, "t": b"mne grustno"},
        )
        token_hash = hashlib.sha256(uuid.uuid4().bytes).digest()
        await s.execute(
            text(
                "INSERT INTO invites (token_hash, couple_id, created_by, expires_at) "
                "VALUES (:h, :c, :a, :e)"
            ),
            {
                "h": token_hash,
                "c": couple,
                "a": a.user_id,
                "e": datetime.now(UTC) + timedelta(days=7),
            },
        )
    return World(a, b, c, couple, msg, token_hash)


async def _ids(s: AsyncSession, sql: str) -> set[uuid.UUID]:
    return {r[0] for r in await s.execute(text(sql))}


PRIVATE_QUERIES = {
    "agents": "SELECT user_id FROM agents",
    "conversations": "SELECT a.user_id FROM conversations c JOIN agents a ON a.id = c.agent_id",
    "memory_items": "SELECT a.user_id FROM memory_items m JOIN agents a ON a.id = m.agent_id",
    "message_drafts": "SELECT a.user_id FROM message_drafts d JOIN agents a ON a.id = d.agent_id",
    "users": "SELECT id FROM users",
}


@pytest.mark.parametrize("table", sorted(PRIVATE_QUERIES))
async def test_private_data_visible_only_to_owner(
    world: World, sessionmaker: async_sessionmaker[AsyncSession], table: str
) -> None:
    """Личный чат, черновики и память читает только владелец (ТЗ §7); партнёр — тоже нет."""
    for person in (world.a, world.b, world.c):
        async with user_scope(sessionmaker, person.user_id) as s:
            assert await _ids(s, PRIVATE_QUERIES[table]) == {person.user_id}


async def test_partner_cannot_read_owner_conversation_messages(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Исповедь приватна (ТЗ §2 п.4, §9.2 п.1): реплики Анны не видны Игорю даже по прямому id."""
    async with user_scope(sessionmaker, world.b.user_id) as s:
        rows = await s.execute(
            text("SELECT count(*) FROM conversation_messages WHERE conversation_id = :c"),
            {"c": world.a.conversation_id},
        )
        assert rows.scalar_one() == 0
    async with user_scope(sessionmaker, world.a.user_id) as s:
        rows = await s.execute(
            text("SELECT count(*) FROM conversation_messages WHERE conversation_id = :c"),
            {"c": world.a.conversation_id},
        )
        assert rows.scalar_one() == 1


async def test_partner_messages_visible_to_couple_only(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Сообщения пары читают оба участника и никто больше (ТЗ §7)."""
    q = "SELECT id FROM partner_messages"
    for person, expected in (
        (world.a, {world.message_id}),
        (world.b, {world.message_id}),
        (world.c, set()),
    ):
        async with user_scope(sessionmaker, person.user_id) as s:
            assert await _ids(s, q) == expected


async def test_outsider_sees_no_couple(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with user_scope(sessionmaker, world.c.user_id) as s:
        assert await _ids(s, "SELECT id FROM couples") == set()
        assert await _ids(s, "SELECT user_id FROM couple_members") == set()
    async with user_scope(sessionmaker, world.b.user_id) as s:
        assert await _ids(s, "SELECT user_id FROM couple_members") == {
            world.a.user_id,
            world.b.user_id,
        }


async def test_no_user_context_sees_nothing(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with user_scope(sessionmaker, None) as s:
        for sql in PRIVATE_QUERIES.values():
            assert await _ids(s, sql) == set()
        assert await _ids(s, "SELECT id FROM partner_messages") == set()


async def test_cannot_write_into_partner_memory(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Память одного агента никогда не попадает к другому (ТЗ §6.3) — в том числе записью."""
    with pytest.raises(ProgrammingError, match="row-level security"):
        async with user_scope(sessionmaker, world.a.user_id) as s:
            await s.execute(
                text(
                    "INSERT INTO memory_items (agent_id, type, text_enc, source) "
                    "VALUES (:a, 'fact', 'x', 'owner_dialog')"
                ),
                {"a": world.b.agent_id},
            )


async def test_cannot_update_or_delete_partner_memory(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with user_scope(sessionmaker, world.a.user_id) as s:
        upd = await s.execute(
            text("UPDATE memory_items SET confidence = 0 WHERE agent_id = :a"),
            {"a": world.b.agent_id},
        )
        dele = await s.execute(
            text("DELETE FROM memory_items WHERE agent_id = :a"), {"a": world.b.agent_id}
        )
        assert upd.rowcount == 0  # type: ignore[attr-defined]
        assert dele.rowcount == 0  # type: ignore[attr-defined]


async def test_cannot_send_message_as_partner(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Слова остаются вашими (ТЗ §2 п.2): нельзя создать сообщение от имени партнёра."""
    with pytest.raises(ProgrammingError, match="row-level security"):
        async with user_scope(sessionmaker, world.a.user_id) as s:
            await s.execute(
                text(
                    "INSERT INTO partner_messages (couple_id, author_id, recipient_id, "
                    "original_enc) VALUES (:c, :b, :a, 'x')"
                ),
                {"c": world.couple_id, "a": world.a.user_id, "b": world.b.user_id},
            )


async def test_outsider_cannot_send_into_couple(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    with pytest.raises(ProgrammingError, match="row-level security"):
        async with user_scope(sessionmaker, world.c.user_id) as s:
            await s.execute(
                text(
                    "INSERT INTO partner_messages (couple_id, author_id, recipient_id, "
                    "original_enc) VALUES (:k, :c, :a, 'x')"
                ),
                {"k": world.couple_id, "c": world.c.user_id, "a": world.a.user_id},
            )


async def test_member_can_send_within_active_couple(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with user_scope(sessionmaker, world.b.user_id) as s:
        await s.execute(
            text(
                "INSERT INTO partner_messages (id, couple_id, author_id, recipient_id, "
                "original_enc) VALUES (:m, :c, :b, :a, 'otvet')"
            ),
            {"m": uuid.uuid4(), "c": world.couple_id, "a": world.a.user_id, "b": world.b.user_id},
        )


async def test_no_sending_after_leaving_couple_but_history_readable(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """После выхода из пары обмен прекращается (ТЗ §4.5 п.2), история читается (допущение Д7)."""
    async with user_scope(sessionmaker, world.a.user_id) as s:
        await s.execute(
            text(
                "UPDATE couple_members SET active = false, left_at = now() "
                "WHERE couple_id = :c AND user_id = :a"
            ),
            {"c": world.couple_id, "a": world.a.user_id},
        )
    with pytest.raises(ProgrammingError, match="row-level security"):
        async with user_scope(sessionmaker, world.b.user_id) as s:
            await s.execute(
                text(
                    "INSERT INTO partner_messages (couple_id, author_id, recipient_id, "
                    "original_enc) VALUES (:c, :b, :a, 'x')"
                ),
                {"c": world.couple_id, "a": world.a.user_id, "b": world.b.user_id},
            )
    async with user_scope(sessionmaker, world.a.user_id) as s:
        assert await _ids(s, "SELECT id FROM partner_messages") == {world.message_id}


async def test_member_cannot_change_partner_membership(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with user_scope(sessionmaker, world.a.user_id) as s:
        res = await s.execute(
            text("UPDATE couple_members SET active = false WHERE user_id = :b"),
            {"b": world.b.user_id},
        )
        assert res.rowcount == 0  # type: ignore[attr-defined]


async def test_safety_events_write_only_without_text(
    world: World, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """safety_events: без текста переписки (§7, §9.3); приложение пишет, но не читает."""
    async with user_scope(sessionmaker, world.a.user_id) as s:
        await s.execute(
            text("INSERT INTO safety_events (id, user_id, type) VALUES (:i, :u, 'self_harm')"),
            {"i": uuid.uuid4(), "u": world.a.user_id},
        )
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with user_scope(sessionmaker, world.a.user_id) as s:
            await s.execute(text("SELECT * FROM safety_events"))
    async with engine.connect() as conn:
        cols = await conn.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
            {"t": "safety_events"},
        )
        assert {r[0] for r in cols} == {"id", "user_id", "type", "created_at"}


async def test_admin_role_has_no_access_to_conversations(engine: AsyncEngine) -> None:
    """Администратор не имеет доступа к перепискам (ТЗ §3, §9.1 п.5)."""
    personal = [
        "users",
        "conversation_messages",
        "memory_items",
        "partner_messages",
        "message_drafts",
        "conversations",
        "agents",
        "safety_events",
    ]
    async with engine.connect() as conn:
        for table in personal:
            row = await conn.execute(
                text("SELECT has_table_privilege('feelings_admin', :t, 'SELECT')"), {"t": table}
            )
            assert row.scalar_one() is False, table
        row = await conn.execute(
            text("SELECT has_table_privilege('feelings_admin', 'kb_documents', 'INSERT')")
        )
        assert row.scalar_one() is True


async def test_invite_lookup_by_token_for_invitee(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Приглашённый ещё не участник: напрямую invites не видит, по токену — находит."""
    async with user_scope(sessionmaker, world.c.user_id) as s:
        assert await _ids(s, "SELECT id FROM invites") == set()
        rows = (
            await s.execute(
                text("SELECT couple_id FROM app_find_invite(:h)"), {"h": world.invite_token_hash}
            )
        ).all()
        assert [r[0] for r in rows] == [world.couple_id]


async def test_upsert_user_is_idempotent(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    tg = next(_tg)
    async with user_scope(sessionmaker, None) as s:
        first = (await s.execute(text("SELECT app_upsert_user(:t)"), {"t": tg})).scalar_one()
        second = (await s.execute(text("SELECT app_upsert_user(:t)"), {"t": tg})).scalar_one()
    assert first == second


async def test_one_active_couple_per_user(world: World, engine: AsyncEngine) -> None:
    """Один пользователь — одна активная пара (ФТ-04, ТЗ §4.1 п.6)."""
    sm = async_sessionmaker(engine)
    with pytest.raises(IntegrityError, match="uq_couple_members_one_active_per_user"):
        async with sm() as s, s.begin():
            other = uuid.uuid4()
            await s.execute(text("INSERT INTO couples (id) VALUES (:c)"), {"c": other})
            await s.execute(
                text(
                    "INSERT INTO couple_members (couple_id, user_id, role) "
                    "VALUES (:c, :u, 'initiator')"
                ),
                {"c": other, "u": world.a.user_id},
            )


async def test_couple_has_at_most_two_members(world: World, engine: AsyncEngine) -> None:
    """Ровно две записи на активную пару (ТЗ §7)."""
    sm = async_sessionmaker(engine)
    with pytest.raises(IntegrityError, match="uq_couple_members_couple_role"):
        async with sm() as s, s.begin():
            await s.execute(
                text(
                    "INSERT INTO couple_members (couple_id, user_id, role) "
                    "VALUES (:c, :u, 'partner')"
                ),
                {"c": world.couple_id, "u": world.c.user_id},
            )


async def test_deleted_memory_keeps_no_content(world: World, engine: AsyncEngine) -> None:
    """Удалённая запись памяти не хранит текст и вектор (ТЗ §6.3, приёмка §12 п.7)."""
    sm = async_sessionmaker(engine)
    with pytest.raises(IntegrityError, match="deleted_has_no_content"):
        async with sm() as s, s.begin():
            await s.execute(
                text("UPDATE memory_items SET deleted_at = now() WHERE agent_id = :a"),
                {"a": world.a.agent_id},
            )
    async with sm() as s, s.begin():
        await s.execute(
            text(
                "UPDATE memory_items SET deleted_at = now(), text_enc = NULL, embedding = NULL "
                "WHERE agent_id = :a"
            ),
            {"a": world.a.agent_id},
        )


async def test_text_columns_are_binary(engine: AsyncEngine) -> None:
    """Тексты диалогов, памяти и сообщений хранятся в зашифрованном виде (ТЗ §9.1 п.3)."""
    expected = {
        ("conversation_messages", "text_enc"),
        ("memory_items", "text_enc"),
        ("partner_messages", "original_enc"),
        ("partner_messages", "adapted_enc"),
        ("message_drafts", "text_enc"),
        ("conversations", "summary_enc"),
        ("agents", "profile_summary_enc"),
    }
    async with engine.connect() as conn:
        for table, col in expected:
            row = await conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": col},
            )
            assert row.scalar_one() == "bytea", (table, col)
        # Ни одной текстовой колонки с содержимым переписки в личных таблицах.
        rows = await conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND data_type = 'text' AND table_name IN "
                "('conversation_messages','memory_items','partner_messages','message_drafts',"
                "'conversations','agents','safety_events','audit_log')"
            )
        )
        assert rows.all() == []


async def test_cannot_join_couple_by_known_id(
    world: World, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """Вступить в пару можно только по приглашению (ФТ-03), не зная лишь id пары."""
    sm = async_sessionmaker(engine)
    pending = uuid.uuid4()
    outsider = uuid.uuid4()
    async with sm() as s, s.begin():
        await s.execute(
            text("INSERT INTO users (id, telegram_id) VALUES (:u, :t)"),
            {"u": outsider, "t": next(_tg)},
        )
        await s.execute(text("INSERT INTO couples (id) VALUES (:c)"), {"c": pending})
        await s.execute(
            text(
                "INSERT INTO couple_members (couple_id, user_id, role) VALUES (:c, :u, 'initiator')"
            ),
            {"c": pending, "u": world.c.user_id},
        )
    for role in ("partner", "initiator"):
        with pytest.raises(ProgrammingError, match="row-level security"):
            async with user_scope(sessionmaker, outsider) as s:
                await s.execute(
                    text(
                        "INSERT INTO couple_members (couple_id, user_id, role) VALUES (:c, :u, :r)"
                    ),
                    {"c": pending, "u": outsider, "r": role},
                )


async def test_member_cannot_activate_couple_alone(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Активация пары — только при принятии приглашения (ФТ-03), не самим инициатором."""
    with pytest.raises(ProgrammingError, match="row-level security"):
        async with user_scope(sessionmaker, world.a.user_id) as s:
            await s.execute(
                text("UPDATE couples SET status = 'active' WHERE id = :c"),
                {"c": world.couple_id},
            )


async def test_left_member_cannot_rejoin_by_update(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with user_scope(sessionmaker, world.a.user_id) as s:
        await s.execute(
            text("UPDATE couple_members SET active = false WHERE user_id = :a"),
            {"a": world.a.user_id},
        )
    with pytest.raises(ProgrammingError, match="row-level security"):
        async with user_scope(sessionmaker, world.a.user_id) as s:
            await s.execute(
                text("UPDATE couple_members SET active = true WHERE user_id = :a"),
                {"a": world.a.user_id},
            )

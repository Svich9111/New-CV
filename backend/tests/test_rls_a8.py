"""0.7, A8: дополнительные негативные тесты изоляции (ТЗ §6.3, §7 «Правила доступа», §9.1, §9.2).

Сценарий: Анна (A) и Игорь (B) — активная пара с подпиской и платежом; Ольга (C) —
посторонняя со своей ожидающей парой. Данные заводит суперпользователь (в обход RLS),
проверки — через `user_scope` (роль feelings_app + app.user_id), как в приложении.

Тесты, помеченные xfail(strict=True), фиксируют дефекты продукта из docs/reviews/0.7-test.md:
когда дефект исправят, тест станет XPASS и упадёт — пометку нужно снять.
"""

import hashlib
import itertools
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    await s.execute(
        text("INSERT INTO users (id, telegram_id, name) VALUES (:i, :t, :n)"),
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
            "INSERT INTO memory_items (id, agent_id, type, text_enc, source) "
            "VALUES (:i, :a, 'fact', :t, 'owner_dialog')"
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
    ("table", "attr", "setter"),
    [
        ("conversation_messages", "conv_message_id", "text_enc = 'x'"),
        ("message_drafts", "draft_id", "text_enc = 'x', status = 'approved'"),
        ("memory_items", "memory_id", "text_enc = 'x'"),
        ("conversations", "conversation_id", "summary_enc = 'x'"),
        ("agents", "agent_id", "profile_summary_enc = 'x'"),
    ],
)
async def test_partner_cannot_update_or_delete_private_rows(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], table: str, attr: str, setter: str
) -> None:
    """Чужие реплики, черновики, память, агент — ни изменить, ни удалить (ТЗ §7, §6.3)."""
    target = getattr(w.a, attr)
    async with user_scope(sessionmaker, w.b.user_id) as s:
        upd = await s.execute(text(f"UPDATE {table} SET {setter} WHERE id = :i"), {"i": target})
        dele = await s.execute(text(f"DELETE FROM {table} WHERE id = :i"), {"i": target})
        assert upd.rowcount == 0  # type: ignore[attr-defined]
        assert dele.rowcount == 0  # type: ignore[attr-defined]
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


# --- Подписки и платежи пары: ФТ-19, ТЗ §7 --------------------------------------------------


async def test_outsider_cannot_read_or_touch_couple_subscription_and_payments(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with user_scope(sessionmaker, w.c.user_id) as s:
        assert await _count(s, "SELECT count(*) FROM subscriptions") == 0
        assert await _count(s, "SELECT count(*) FROM payments") == 0
        upd = await s.execute(
            text("UPDATE subscriptions SET status = 'canceled' WHERE id = :i"),
            {"i": w.subscription_id},
        )
        assert upd.rowcount == 0  # type: ignore[attr-defined]
    with pytest.raises(ProgrammingError, match=RLS_DENIED):
        async with user_scope(sessionmaker, w.c.user_id) as s:
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


# --- partner_messages: ФТ-08, ФТ-10, ТЗ §2 п.2, §4.3 п.3, §7 ---------------------------------


async def test_partner_message_cannot_be_deleted_by_participants(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    for p in (w.a, w.b):
        async with user_scope(sessionmaker, p.user_id) as s:
            res = await s.execute(
                text("DELETE FROM partner_messages WHERE id = :m"), {"m": w.message_id}
            )
            assert res.rowcount == 0  # type: ignore[attr-defined]


async def test_outsider_cannot_update_partner_message(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with user_scope(sessionmaker, w.c.user_id) as s:
        res = await s.execute(
            text("UPDATE partner_messages SET recipient_id = :c WHERE id = :m"),
            {"c": w.c.user_id, "m": w.message_id},
        )
        assert res.rowcount == 0  # type: ignore[attr-defined]


async def test_recipient_can_mark_read(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Получатель отмечает прочтение и просмотр оригинала (ФТ-10, ФТ-11) — это допустимо."""
    async with user_scope(sessionmaker, w.b.user_id) as s:
        res = await s.execute(
            text(
                "UPDATE partner_messages SET status = 'read', read_at = now(), "
                "original_viewed_at = now() WHERE id = :m"
            ),
            {"m": w.message_id},
        )
        assert res.rowcount == 1  # type: ignore[attr-defined]


@pytest.mark.xfail(reason="дефект D-1, см. docs/reviews/0.7-test.md", strict=True)
async def test_recipient_cannot_forge_author(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Получатель не может подменить автора сообщения (ТЗ §2 п.2, ФТ-08)."""
    with pytest.raises(ProgrammingError, match=RLS_DENIED):
        async with user_scope(sessionmaker, w.b.user_id) as s:
            await s.execute(
                text("UPDATE partner_messages SET author_id = :c WHERE id = :m"),
                {"c": w.c.user_id, "m": w.message_id},
            )


@pytest.mark.xfail(reason="дефект D-1, см. docs/reviews/0.7-test.md", strict=True)
async def test_original_is_immutable(w: W, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Оригинал, одобренный автором, не меняется ни получателем, ни автором (ТЗ §4.3 п.3, ФТ-10)."""
    for p in (w.b, w.a):
        async with user_scope(sessionmaker, p.user_id) as s:
            await s.execute(
                text("UPDATE partner_messages SET original_enc = 'forged' WHERE id = :m"),
                {"m": w.message_id},
            )
            orig = (
                await s.execute(
                    text("SELECT original_enc FROM partner_messages WHERE id = :m"),
                    {"m": w.message_id},
                )
            ).scalar_one()
            assert orig == b"original-A"


@pytest.mark.xfail(reason="дефект D-1, см. docs/reviews/0.7-test.md", strict=True)
async def test_author_cannot_redirect_message_to_outsider(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Сообщения пары читают только двое участников (ТЗ §7): автор не может «переадресовать»
    отправленное постороннему или перенести его в чужую пару."""
    async with user_scope(sessionmaker, w.a.user_id) as s:
        await s.execute(
            text("UPDATE partner_messages SET recipient_id = :c, couple_id = :o WHERE id = :m"),
            {"c": w.c.user_id, "o": w.c_couple_id, "m": w.message_id},
        )
    async with user_scope(sessionmaker, w.c.user_id) as s:
        assert await _count(s, "SELECT count(*) FROM partner_messages") == 0


# --- Пары и приглашения: ФТ-03, ФТ-04, ТЗ §4.1 ---------------------------------------------


async def test_outsider_cannot_create_invite_into_foreign_couple(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    with pytest.raises(ProgrammingError, match=RLS_DENIED):
        async with user_scope(sessionmaker, w.c.user_id) as s:
            await s.execute(
                text(
                    "INSERT INTO invites (token_hash, couple_id, created_by, expires_at) "
                    "VALUES ('\\x01', :k, :c, now() + interval '7 days')"
                ),
                {"k": w.couple_id, "c": w.c.user_id},
            )


async def test_cannot_create_invite_in_someone_elses_name(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    with pytest.raises(ProgrammingError, match=RLS_DENIED):
        async with user_scope(sessionmaker, w.b.user_id) as s:
            await s.execute(
                text(
                    "INSERT INTO invites (token_hash, couple_id, created_by, expires_at) "
                    "VALUES ('\\x02', :k, :a, now() + interval '7 days')"
                ),
                {"k": w.couple_id, "a": w.a.user_id},
            )


async def test_partner_cannot_see_or_revoke_invites_of_other(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    for p in (w.a, w.b):
        async with user_scope(sessionmaker, p.user_id) as s:
            assert await _count(s, "SELECT count(*) FROM invites") == 0
            res = await s.execute(text("UPDATE invites SET status = 'revoked'"))
            assert res.rowcount == 0  # type: ignore[attr-defined]


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
    with pytest.raises(ProgrammingError, match=RLS_DENIED):
        async with user_scope(sessionmaker, w.c.user_id) as s:
            await s.execute(
                text(
                    "UPDATE couple_members SET couple_id = :k, role = 'partner', active = false "
                    "WHERE user_id = :c"
                ),
                {"k": lonely, "c": w.c.user_id},
            )


async def test_couple_rows_cannot_be_deleted_by_member(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with user_scope(sessionmaker, w.a.user_id) as s:
        r1 = await s.execute(text("DELETE FROM couples WHERE id = :k"), {"k": w.couple_id})
        r2 = await s.execute(
            text("DELETE FROM couple_members WHERE couple_id = :k"), {"k": w.couple_id}
        )
        assert r1.rowcount == 0  # type: ignore[attr-defined]
        assert r2.rowcount == 0  # type: ignore[attr-defined]


@pytest.mark.xfail(reason="дефект D-4, см. docs/reviews/0.7-test.md", strict=True)
async def test_former_member_cannot_invite_into_former_couple(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """После выхода обмен прекращается (ТЗ §4.5 п.2): бывший участник не приглашает в пару."""
    async with user_scope(sessionmaker, w.a.user_id) as s:
        await s.execute(
            text("UPDATE couple_members SET active = false, left_at = now() WHERE user_id = :a"),
            {"a": w.a.user_id},
        )
    with pytest.raises(ProgrammingError, match=RLS_DENIED):
        async with user_scope(sessionmaker, w.a.user_id) as s:
            await s.execute(
                text(
                    "INSERT INTO invites (token_hash, couple_id, created_by, expires_at) "
                    "VALUES ('\\x03', :k, :a, now() + interval '7 days')"
                ),
                {"k": w.couple_id, "a": w.a.user_id},
            )


# --- Функции SECURITY DEFINER: АР-7, ТЗ §9.2 п.1 ---------------------------------------------


async def test_find_invite_reveals_only_by_exact_token(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with user_scope(sessionmaker, w.a.user_id) as s:
        res = await s.execute(text("SELECT * FROM app_find_invite(:h)"), {"h": w.invite_token_hash})
        assert list(res.keys()) == ["id", "couple_id", "created_by", "expires_at", "status"]
        assert len(res.all()) == 1
        for h in (None, b"", w.invite_token_hash[:-1], hashlib.sha256(b"guess").digest()):
            assert await _count(s, "SELECT count(*) FROM app_find_invite(:h)", h=h) == 0
        # Лишнего (хеш токена, принявший) не отдаёт, и сама invites чужой стороне не видна.
        assert await _count(s, "SELECT count(*) FROM invites") == 0


async def test_definer_functions_not_executable_by_public_or_admin(engine: AsyncEngine) -> None:
    funcs = [
        "app_upsert_user(bigint)",
        "app_find_invite(bytea)",
        "app_is_member(uuid)",
        "app_is_active_member(uuid, uuid)",
        "app_couple_has_members(uuid)",
    ]
    async with engine.connect() as conn:
        for f in funcs:
            for role in ("feelings_admin", "public"):
                row = await conn.execute(
                    text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"), {"r": role, "f": f}
                )
                assert row.scalar_one() is False, (role, f)
        rows = await conn.execute(
            text(
                "SELECT p.proname, p.proconfig FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public' AND p.prosecdef"
            )
        )
        for name, cfg in rows:
            assert cfg and any(c.startswith("search_path=") for c in cfg), name


@pytest.mark.xfail(reason="дефект D-2, см. docs/reviews/0.7-test.md", strict=True)
async def test_definer_functions_cannot_be_hijacked_by_temp_tables(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Временная таблица couple_members/couples в pg_temp не должна подменять настоящую внутри
    функций SECURITY DEFINER (search_path без pg_temp в конце): иначе посторонний «становится»
    участником чужой пары и видит её, её подписку и пишет в неё (ТЗ §7, §9.2 п.1)."""
    async with user_scope(sessionmaker, w.c.user_id) as s:
        await s.execute(
            text(
                "CREATE TEMP TABLE couple_members (couple_id uuid, user_id uuid, active bool) "
                "ON COMMIT DROP"
            )
        )
        await s.execute(text("CREATE TEMP TABLE couples (id uuid, status text) ON COMMIT DROP"))
        await s.execute(
            text("INSERT INTO pg_temp.couple_members VALUES (:k, :c, true), (:k, :a, true)"),
            {"k": w.couple_id, "c": w.c.user_id, "a": w.a.user_id},
        )
        await s.execute(
            text("INSERT INTO pg_temp.couples VALUES (:k, 'active')"), {"k": w.couple_id}
        )
        seen = await _count(s, "SELECT count(*) FROM public.couples WHERE id = :k", k=w.couple_id)
        subs = await _count(s, "SELECT count(*) FROM public.subscriptions")
        assert (seen, subs) == (0, 0)


@pytest.mark.xfail(reason="дефект D-2, см. docs/reviews/0.7-test.md", strict=True)
async def test_temp_table_hijack_cannot_send_into_foreign_couple(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """То же подложение pg_temp позволяет постороннему отправить сообщение в чужую пару."""
    with pytest.raises(ProgrammingError, match=RLS_DENIED):
        async with user_scope(sessionmaker, w.c.user_id) as s:
            await s.execute(
                text(
                    "CREATE TEMP TABLE couple_members (couple_id uuid, user_id uuid, "
                    "active bool) ON COMMIT DROP"
                )
            )
            await s.execute(text("CREATE TEMP TABLE couples (id uuid, status text) ON COMMIT DROP"))
            await s.execute(
                text("INSERT INTO pg_temp.couple_members VALUES (:k, :c, true), (:k, :a, true)"),
                {"k": w.couple_id, "c": w.c.user_id, "a": w.a.user_id},
            )
            await s.execute(
                text("INSERT INTO pg_temp.couples VALUES (:k, 'active')"), {"k": w.couple_id}
            )
            await s.execute(
                text(
                    "INSERT INTO public.partner_messages (couple_id, author_id, recipient_id, "
                    "original_enc) VALUES (:k, :c, :a, 'spam')"
                ),
                {"k": w.couple_id, "c": w.c.user_id, "a": w.a.user_id},
            )


@pytest.mark.xfail(reason="дефект D-6, см. docs/reviews/0.7-test.md", strict=True)
async def test_membership_oracle_not_available_for_foreign_users(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Посторонний не должен узнавать, кто состоит в чужой паре (ТЗ §7, §9.2 п.2)."""
    async with user_scope(sessionmaker, w.c.user_id) as s:
        known = (
            await s.execute(
                text("SELECT app_is_active_member(:k, :a)"), {"k": w.couple_id, "a": w.a.user_id}
            )
        ).scalar_one()
        assert known is False


# --- Роль приложения: нельзя выйти из-под RLS (АР-7, ТЗ §9.1 п.5, §9.2 п.4) -----------------


async def test_app_role_cannot_disable_rls_or_alter_policies(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    for ddl in (
        "ALTER TABLE memory_items DISABLE ROW LEVEL SECURITY",
        "DROP POLICY memory_owner ON memory_items",
        "ALTER TABLE memory_items NO FORCE ROW LEVEL SECURITY",
        "SET LOCAL row_security = off",
    ):
        with pytest.raises(DBAPIError):
            async with user_scope(sessionmaker, w.b.user_id) as s:
                await s.execute(text(ddl))
                # row_security=off под не-владельцем не отключает RLS, а даёт ошибку на запросе.
                await s.execute(text("SELECT count(*) FROM memory_items"))


async def test_app_role_has_no_bypassrls_and_owns_nothing(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        r = await conn.execute(
            text("SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname = :r"),
            {"r": "feelings_app"},
        )
        assert tuple(r.one()) == (False, False, False)
        owned = await conn.execute(
            text(
                "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
                "WHERE r.rolname IN ('feelings_app', 'feelings_admin')"
            )
        )
        assert owned.scalar_one() == 0


async def test_dedicated_login_role_cannot_escape_to_superuser(engine: AsyncEngine) -> None:
    """При входе под отдельной ролью (член только feelings_app) SET ROLE postgres невозможен —
    так и должно быть развёрнуто (см. D-3)."""
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


@pytest.mark.xfail(reason="дефект D-3, см. docs/reviews/0.7-test.md", strict=True)
async def test_user_scope_cannot_escape_role(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Внутри user_scope роль приложения не должна иметь возможность вернуться к роли входа
    (RESET ROLE / SET ROLE postgres) и прочитать чужую память."""
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with user_scope(sessionmaker, w.b.user_id) as s:
            await s.execute(text("SET LOCAL ROLE postgres"))


# --- Мутационная проверка: тесты действительно ловят отключённый RLS ------------------------


async def test_mutation_disabled_rls_is_detected(w: W, engine: AsyncEngine) -> None:
    """В одной транзакции (откатывается) отключаем RLS на memory_items и проверяем, что та же
    проверка, что в тестах изоляции, видит чужую память. Значит, зелёные тесты изоляции —
    заслуга политик, а не пустых данных."""
    probe = "SELECT count(*) FROM memory_items WHERE id = :i"
    sm = async_sessionmaker(engine)
    async with sm() as s:
        await s.begin()
        await s.execute(text("ALTER TABLE memory_items DISABLE ROW LEVEL SECURITY"))
        await s.execute(text("SET LOCAL ROLE feelings_app"))
        await s.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(w.b.user_id)})
        leaked = await _count(s, probe, i=w.a.memory_id)
        await s.rollback()
    assert leaked == 1
    async with engine.connect() as conn:
        rls = await conn.execute(
            text("SELECT relrowsecurity FROM pg_class WHERE relname = 'memory_items'")
        )
        assert rls.scalar_one() is True


# --- Удаление аккаунта: ФТ-21, ТЗ §4.5 п.3, §7 ---------------------------------------------


async def test_account_deletion_cascades_personal_data(
    w: W, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    async with user_scope(sessionmaker, w.a.user_id) as s:
        await s.execute(
            text("INSERT INTO safety_events (user_id, type) VALUES (:u, 'self_harm')"),
            {"u": w.a.user_id},
        )
        res = await s.execute(text("DELETE FROM users WHERE id = app_uid()"))
        assert res.rowcount == 1  # type: ignore[attr-defined]
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


async def test_cannot_delete_other_user(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with user_scope(sessionmaker, w.b.user_id) as s:
        res = await s.execute(text("DELETE FROM users WHERE id = :a"), {"a": w.a.user_id})
        assert res.rowcount == 0  # type: ignore[attr-defined]


# --- Журналы без чтения: ТЗ §9.1 п.5, §9.3 --------------------------------------------------


async def test_audit_log_write_only_and_no_forged_actor(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with user_scope(sessionmaker, w.a.user_id) as s:
        await s.execute(
            text("INSERT INTO audit_log (actor_id, action, object_type) VALUES (:a, 'x', 'y')"),
            {"a": w.a.user_id},
        )
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with user_scope(sessionmaker, w.a.user_id) as s:
            await s.execute(text("SELECT count(*) FROM audit_log"))
    with pytest.raises(ProgrammingError, match=RLS_DENIED):
        async with user_scope(sessionmaker, w.a.user_id) as s:
            await s.execute(
                text("INSERT INTO audit_log (actor_id, action, object_type) VALUES (:b, 'x', 'y')"),
                {"b": w.b.user_id},
            )
    with pytest.raises(ProgrammingError, match=RLS_DENIED):
        async with user_scope(sessionmaker, w.a.user_id) as s:
            await s.execute(
                text("INSERT INTO safety_events (user_id, type) VALUES (:b, 'x')"),
                {"b": w.b.user_id},
            )


# --- Вход и учётная запись: ФТ-01, ФТ-21 ---------------------------------------------------


@pytest.mark.xfail(reason="дефект D-5, см. docs/reviews/0.7-test.md", strict=True)
async def test_user_cannot_change_own_telegram_id(
    w: W, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """telegram_id — ключ входа (ФТ-01): пользователь не должен переписывать его на чужой
    (будущий) id, иначе тот при входе попадёт в чужую учётную запись."""
    with pytest.raises(ProgrammingError):
        async with user_scope(sessionmaker, w.a.user_id) as s:
            await s.execute(
                text("UPDATE users SET telegram_id = :t WHERE id = app_uid()"), {"t": next(_tg)}
            )


@pytest.mark.xfail(reason="дефект D-7, см. docs/reviews/0.7-test.md", strict=True)
async def test_upsert_user_after_soft_delete_returns_account(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """После удаления аккаунта (ФТ-21) человек может войти снова; app_upsert_user не должен
    молча возвращать NULL на время 30-дневного окна удаления."""
    tg = next(_tg)
    sm = async_sessionmaker(engine)
    async with sm() as s, s.begin():
        await s.execute(
            text("INSERT INTO users (telegram_id, deleted_at) VALUES (:t, now())"), {"t": tg}
        )
    async with user_scope(sessionmaker, None) as s:
        uid = (await s.execute(text("SELECT app_upsert_user(:t)"), {"t": tg})).scalar_one()
    assert uid is not None

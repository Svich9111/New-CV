"""0.7, раунд 2: сообщения пары, пары и подписки меняются только через функции
(ревью 0.7: B1, M2–M5; тесты 0.7: D-1–D-8). ТЗ §2 п.2–3, §6.2, §7, §9.2, ФТ-08, ФТ-10, ФТ-11."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db import user_scope
from tests import _pipeline_helpers as pipeline
from tests.conftest import app_database_url
from tests.test_rls_isolation import World, _draft_id

DENIED = r"permission denied|row-level security"
# Пару создаёт только прошедший онбординг: согласия и 18+ (0003, FB008; ТЗ §4.1 п.2).
ONBOARDED_USER_SQL = (
    "INSERT INTO users (id, telegram_id, consent_terms_at, consent_data_at, "
    "consent_sensitive_at, age_confirmed_at) VALUES (:u, :t, now(), now(), now(), now())"
)


async def _send(sm: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> uuid.UUID:
    async with user_scope(sm, uid) as s:
        draft = await _draft_id(s)
        value = (await s.execute(text("SELECT app_send_draft(:d, 1)"), {"d": draft})).scalar_one()
        return uuid.UUID(str(value))


async def _deliver(engine: AsyncEngine, msg: uuid.UUID, text_for_recipient: bytes) -> None:
    """Конвейер как у воркера: захват → доставка по токену (Б-83)."""
    ok = await pipeline.deliver_blob(engine, msg, text_for_recipient, is_adapted=True, tone=2)
    assert ok == "delivered"


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE partner_messages SET recipient_id = :c WHERE id = :m",
        "UPDATE partner_messages SET author_id = :c WHERE id = :m",
        "UPDATE partner_messages SET original_enc = 'x' WHERE id = :m",
        "UPDATE partner_messages SET status = 'delivered' WHERE id = :m",
        "DELETE FROM partner_messages WHERE id = :m",
    ],
)
async def test_participants_cannot_modify_partner_messages(
    world: World, sessionmaker: async_sessionmaker[AsyncSession], sql: str
) -> None:
    """B1/D-1: ни автор, ни получатель не меняют сообщение напрямую."""
    for person in (world.a, world.b):
        with pytest.raises(ProgrammingError, match=DENIED):
            async with user_scope(sessionmaker, person.user_id) as s:
                await s.execute(text(sql), {"m": world.message_id, "c": world.c.user_id})


async def test_original_not_selectable_directly(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """M5: оригинал отдаётся получателю только отдельным запросом (ТЗ §7, ФТ-10)."""
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with user_scope(sessionmaker, world.b.user_id) as s:
            await s.execute(text("SELECT original_enc FROM partner_messages"))


async def test_recipient_does_not_see_message_before_pipeline(
    world: World, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """M5: пока конвейер не закончил, получатель сообщения не видит; после — видит версию для себя."""
    msg = await _send(sessionmaker, world.a.user_id)
    async with user_scope(sessionmaker, world.b.user_id) as s:
        ids = {r[0] for r in await s.execute(text("SELECT id FROM partner_messages"))}
        assert msg not in ids
        assert (
            await s.execute(text("SELECT app_read_original(:m)"), {"m": msg})
        ).scalar_one() is None
    await _deliver(engine, msg, b"adapted")
    async with user_scope(sessionmaker, world.b.user_id) as s:
        feed = (
            await s.execute(
                text("SELECT id, outgoing, text_enc, is_adapted FROM app_couple_feed(50, NULL)")
            )
        ).all()
        mine = [r for r in feed if r.id == msg]
        assert len(mine) == 1
        assert mine[0].outgoing is False
        assert bytes(mine[0].text_enc) == b"adapted"
        assert mine[0].is_adapted is True


async def test_author_feed_shows_own_original(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    msg = await _send(sessionmaker, world.a.user_id)
    async with user_scope(sessionmaker, world.a.user_id) as s:
        feed = {r.id: r for r in await s.execute(text("SELECT * FROM app_couple_feed(50, NULL)"))}
    assert feed[msg].outgoing is True
    assert bytes(feed[msg].text_enc) == "черновик Анна".encode()


async def test_original_view_is_recorded_for_recipient(
    world: World, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """ФТ-10: «Показать оригинал» фиксируется; посторонний оригинал не получает."""
    msg = await _send(sessionmaker, world.a.user_id)
    await _deliver(engine, msg, b"adapted")
    async with user_scope(sessionmaker, world.c.user_id) as s:
        assert (
            await s.execute(text("SELECT app_read_original(:m)"), {"m": msg})
        ).scalar_one() is None
    async with user_scope(sessionmaker, world.b.user_id) as s:
        original = (await s.execute(text("SELECT app_read_original(:m)"), {"m": msg})).scalar_one()
        assert bytes(original) == "черновик Анна".encode()
        viewed = await s.execute(
            text("SELECT original_viewed_at FROM partner_messages WHERE id = :m"), {"m": msg}
        )
        assert viewed.scalar_one() is not None


async def test_withheld_message_never_reaches_recipient(
    world: World, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """ТЗ §6.2 п.3, §9.3: остановленное проверкой безопасности сообщение получатель не видит."""
    msg = await _send(sessionmaker, world.a.user_id)
    assert await pipeline.withhold(engine, msg)
    async with user_scope(sessionmaker, world.b.user_id) as s:
        assert msg not in {r[0] for r in await s.execute(text("SELECT id FROM partner_messages"))}
        assert (
            await s.execute(text("SELECT app_read_original(:m)"), {"m": msg})
        ).scalar_one() is None


async def test_mark_read_only_by_recipient(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with user_scope(sessionmaker, world.a.user_id) as s:
        assert (
            await s.execute(text("SELECT app_mark_read(:m)"), {"m": world.message_id})
        ).scalar_one() is False
    async with user_scope(sessionmaker, world.b.user_id) as s:
        assert (
            await s.execute(text("SELECT app_mark_read(:m)"), {"m": world.message_id})
        ).scalar_one() is True
        status = await s.execute(
            text("SELECT status FROM partner_messages WHERE id = :m"), {"m": world.message_id}
        )
        assert status.scalar_one() == "read"


async def _reply(sm: async_sessionmaker[AsyncSession], world: World) -> uuid.UUID:
    async with user_scope(sm, world.b.user_id) as s:
        draft = await _draft_id(s)
        await s.execute(
            text("UPDATE message_drafts SET reply_to_id = :m WHERE id = :d"),
            {"m": world.message_id, "d": draft},
        )
        value = (await s.execute(text("SELECT app_send_draft(:d, 1)"), {"d": draft})).scalar_one()
        return uuid.UUID(str(value))


async def _status(sm: async_sessionmaker[AsyncSession], uid: uuid.UUID, msg: uuid.UUID) -> str:
    async with user_scope(sm, uid) as s:
        row = await s.execute(text("SELECT status FROM partner_messages WHERE id = :m"), {"m": msg})
        return str(row.scalar_one())


async def test_reply_marks_incoming_answered_after_delivery(
    world: World, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """M6 / ФТ-11: ответ ставит входящему «отвечено» — когда ответ доставлен."""
    reply = await _reply(sessionmaker, world)
    assert await _status(sessionmaker, world.a.user_id, world.message_id) == "delivered"
    await _deliver(engine, reply, b"otvet")
    assert await _status(sessionmaker, world.a.user_id, world.message_id) == "answered"


async def test_withheld_reply_does_not_reveal_safety_event(
    world: World, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """Ревью 0.7 N1, ТЗ §9.3: остановленный ответ не меняет статус входящего у партнёра."""
    reply = await _reply(sessionmaker, world)
    assert await pipeline.withhold(engine, reply)
    assert await _status(sessionmaker, world.a.user_id, world.message_id) == "delivered"
    async with user_scope(sessionmaker, world.a.user_id) as s:
        answered = await s.execute(
            text("SELECT answered_at FROM partner_messages WHERE id = :m"),
            {"m": world.message_id},
        )
        assert answered.scalar_one() is None


async def test_pipeline_deliver_requires_text(
    world: World, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    msg = await _send(sessionmaker, world.a.user_id)
    c = await pipeline.claim(engine, msg)
    assert c is not None
    # Без версии для получателя доставка отклоняется (22023), сообщение остаётся в конвейере.
    with pytest.raises(DBAPIError, match="no text for recipient"):
        await pipeline.worker_call(engine, pipeline.deliver_blob_leased, msg, c.token, None)
    async with engine.connect() as conn:
        status = await conn.execute(
            text("SELECT status FROM partner_messages WHERE id = :m"), {"m": msg}
        )
        assert status.scalar_one() == "processing"


@pytest.mark.parametrize(
    "extra",
    [
        "now() + interval '10 years', NULL, NULL",
        "now() + interval '7 days', :a, now()",
    ],
)
async def test_invite_insert_limits(
    world: World, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine, extra: str
) -> None:
    """Ревью 0.7 n2, ФТ-03: приглашение — не дольше 7 дней и не «уже использованное»."""
    uid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(ONBOARDED_USER_SQL),
            {"u": uid, "t": 7_100_000_000 + uuid.uuid4().int % 1_000_000},
        )
    async with user_scope(sessionmaker, uid) as s:
        couple = (await s.execute(text("SELECT app_create_couple()"))).scalar_one()
    with pytest.raises(ProgrammingError, match=DENIED):
        async with user_scope(sessionmaker, uid) as s:
            await s.execute(
                text(
                    "INSERT INTO invites (token_hash, couple_id, created_by, expires_at, "
                    f"used_by, used_at) VALUES (:h, :c, :u, {extra})"
                ),
                {"h": uuid.uuid4().bytes * 2, "c": couple, "u": uid, "a": world.a.user_id},
            )


async def test_blocked_draft_cannot_be_reopened(
    world: World, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """Ревью 0.7 n7, ТЗ §9.3: остановленный безопасностью черновик не вернуть в отправку."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE message_drafts SET status = 'blocked' WHERE agent_id = :a"),
            {"a": world.a.agent_id},
        )
    async with user_scope(sessionmaker, world.a.user_id) as s:
        res = await s.execute(
            text("UPDATE message_drafts SET status = 'draft' WHERE agent_id = :a"),
            {"a": world.a.agent_id},
        )
        assert res.rowcount == 0  # type: ignore[attr-defined]


async def test_cannot_reply_to_own_message(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    with pytest.raises(DBAPIError, match="bad reply target"):
        async with user_scope(sessionmaker, world.a.user_id) as s:
            draft = await _draft_id(s)
            await s.execute(
                text("UPDATE message_drafts SET reply_to_id = :m WHERE id = :d"),
                {"m": world.message_id, "d": draft},
            )
            await s.execute(text("SELECT app_send_draft(:d, 1)"), {"d": draft})


async def test_send_requires_seen_version_and_own_draft(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Отправляется ровно та версия, которую видел автор; чужой черновик не отправить."""
    with pytest.raises(DBAPIError, match="version conflict"):
        async with user_scope(sessionmaker, world.a.user_id) as s:
            await s.execute(text("SELECT app_send_draft(:d, 2)"), {"d": await _draft_id(s)})
    async with user_scope(sessionmaker, world.b.user_id) as s:
        b_draft = await _draft_id(s)
    with pytest.raises(DBAPIError, match="draft not sendable"):
        async with user_scope(sessionmaker, world.a.user_id) as s:
            await s.execute(text("SELECT app_send_draft(:d, 1)"), {"d": b_draft})


async def test_sent_draft_is_frozen_and_not_resendable(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _send(sessionmaker, world.a.user_id)
    async with user_scope(sessionmaker, world.a.user_id) as s:
        res = await s.execute(
            text("UPDATE message_drafts SET text_enc = 'x' WHERE status = 'sent'")
        )
        assert res.rowcount == 0  # type: ignore[attr-defined]
    with pytest.raises(DBAPIError, match="draft not sendable"):
        async with user_scope(sessionmaker, world.a.user_id) as s:
            await s.execute(text("SELECT app_send_draft(:d, 1)"), {"d": await _draft_id(s)})


async def test_app_cannot_call_pipeline_functions(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Адаптацию пишет только конвейер (feelings_worker), не клиентская сторона."""
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with user_scope(sessionmaker, world.a.user_id) as s:
            await s.execute(
                text(
                    "SELECT pipeline_deliver_leased(:m, :t, 'x', false, 0::smallint, false, "
                    "NULL::safety_event_type[])"
                ),
                {"m": world.message_id, "t": uuid.uuid4()},
            )


async def test_create_couple_is_pending_and_direct_insert_denied(
    sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """M2/M3: пара создаётся только функцией и только в статусе pending."""
    uid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(ONBOARDED_USER_SQL),
            {"u": uid, "t": 7_000_000_000 + uuid.uuid4().int % 1_000_000},
        )
    with pytest.raises(ProgrammingError, match=DENIED):
        async with user_scope(sessionmaker, uid) as s:
            await s.execute(text("INSERT INTO couples (status) VALUES ('active')"))
    async with user_scope(sessionmaker, uid) as s:
        couple = (await s.execute(text("SELECT app_create_couple()"))).scalar_one()
        status = await s.execute(text("SELECT status FROM couples WHERE id = :c"), {"c": couple})
        assert status.scalar_one() == "pending"
        members = (
            await s.execute(text("SELECT role, active FROM app_couple_members(:c)"), {"c": couple})
        ).all()
        assert [tuple(m) for m in members] == [("initiator", True)]


async def test_couple_members_with_names_only_for_members(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """D-8: имя партнёра доступно участнику пары и недоступно постороннему."""
    async with user_scope(sessionmaker, world.a.user_id) as s:
        names = {
            r.name
            for r in await s.execute(
                text("SELECT * FROM app_couple_members(:c)"), {"c": world.couple_id}
            )
        }
        assert names == {"Анна", "Игорь"}
    async with user_scope(sessionmaker, world.c.user_id) as s:
        rows = (
            await s.execute(text("SELECT * FROM app_couple_members(:c)"), {"c": world.couple_id})
        ).all()
        assert rows == []


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE subscriptions SET period_end = now() + interval '10 years'",
        "INSERT INTO subscriptions (couple_id, plan, status, period_start, period_end) "
        "VALUES (:c, 'month', 'active', now(), now() + interval '1 month')",
        "DELETE FROM payments",
    ],
)
async def test_members_cannot_write_subscriptions(
    world: World, sessionmaker: async_sessionmaker[AsyncSession], sql: str
) -> None:
    """M4: подписку и платежи меняет только платёжная часть (4.4)."""
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with user_scope(sessionmaker, world.a.user_id) as s:
            await s.execute(text(sql), {"c": world.couple_id})


async def test_former_member_cannot_invite(
    world: World, sessionmaker: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    """D-4: после выхода из пары приглашать в неё нельзя (ТЗ §4.5 п.2): прямой INSERT
    отклоняется (с 0004 — правами, Б-13), функция выдачи — FB007."""
    async with engine.begin() as conn:  # онбординг пройден: отказ именно из-за выхода
        await conn.execute(
            text(
                "UPDATE users SET consent_terms_at = now(), consent_data_at = now(), "
                "consent_sensitive_at = now(), age_confirmed_at = now() WHERE id = :a"
            ),
            {"a": world.a.user_id},
        )
    async with user_scope(sessionmaker, world.a.user_id) as s:
        await s.execute(text("SELECT app_leave_couple()"))
    with pytest.raises(DBAPIError, match="no active membership"):
        async with user_scope(sessionmaker, world.a.user_id) as s:
            await s.execute(
                text("SELECT app_issue_invite(:h)"), {"h": uuid.uuid4().bytes + uuid.uuid4().bytes}
            )
    with pytest.raises(ProgrammingError, match=DENIED):
        async with user_scope(sessionmaker, world.a.user_id) as s:
            await s.execute(
                text(
                    "INSERT INTO invites (token_hash, couple_id, created_by, expires_at) "
                    "VALUES (:h, :c, :a, now() + interval '7 days')"
                ),
                {
                    "h": uuid.uuid4().bytes + uuid.uuid4().bytes,
                    "c": world.couple_id,
                    "a": world.a.user_id,
                },
            )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE users SET telegram_id = 1 WHERE id = :u",
        "UPDATE users SET deleted_at = NULL WHERE id = :u",
        "UPDATE invites SET status = 'active', expires_at = now() + interval '1 year'",
    ],
)
async def test_immutable_columns(
    world: World, sessionmaker: async_sessionmaker[AsyncSession], sql: str
) -> None:
    """D-5 / m5: telegram_id, признак удаления и срок приглашения не меняются клиентом."""
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with user_scope(sessionmaker, world.a.user_id) as s:
            await s.execute(text(sql), {"u": world.a.user_id})


async def test_temp_tables_cannot_hijack_definer_functions(
    world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """D-2: временные таблицы запрещены — подменить couple_members нельзя."""
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with user_scope(sessionmaker, world.c.user_id) as s:
            await s.execute(
                text("CREATE TEMP TABLE couple_members (couple_id uuid, user_id uuid, active bool)")
            )


@pytest.mark.parametrize("sql", ["SET ROLE postgres", "RESET ROLE; SELECT * FROM users"])
async def test_login_role_cannot_escape(world: World, app_engine: AsyncEngine, sql: str) -> None:
    """D-3: приложение входит как feelings_api — без суперпользователя и без прав вне user_scope."""
    async with app_engine.connect() as conn:
        with pytest.raises(DBAPIError, match="permission denied"):
            for stmt in sql.split("; "):
                await conn.execute(text(stmt))


async def test_login_role_has_no_direct_privileges(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as conn:
        with pytest.raises(ProgrammingError, match="permission denied"):
            await conn.execute(text("SELECT id FROM users"))


def test_app_url_helper() -> None:
    assert app_database_url("postgresql+asyncpg://u:p@h:5432/db").startswith(
        "postgresql+asyncpg://feelings_api:"
    )

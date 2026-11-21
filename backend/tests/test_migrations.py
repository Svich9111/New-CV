"""0.7: миграция обратима и создаёт 17 таблиц ТЗ §7 с RLS на личных данных."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import TEST_DATABASE_URL, alembic

TZ_TABLES = {
    "users",
    "couples",
    "couple_members",
    "invites",
    "agents",
    "conversations",
    "conversation_messages",
    "memory_items",
    "partner_messages",
    "message_drafts",
    "kb_documents",
    "kb_chunks",
    "safety_events",
    "subscriptions",
    "payments",
    "prompt_versions",
    "audit_log",
}
NON_PERSONAL = {"kb_documents", "kb_chunks", "prompt_versions"}


async def _tables() -> dict[str, bool]:
    eng = create_async_engine(TEST_DATABASE_URL)
    async with eng.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT tablename, rowsecurity FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        )
        result = {r[0]: r[1] for r in rows}
    await eng.dispose()
    return result


async def test_upgrade_downgrade_upgrade(migrated_db: str) -> None:
    alembic("downgrade", "base")
    assert await _tables() == {}
    alembic("upgrade", "head")
    tables = await _tables()
    assert set(tables) == TZ_TABLES
    for name, rls in tables.items():
        assert rls == (name not in NON_PERSONAL), name

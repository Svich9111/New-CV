"""2.4 (A1): долговременная память на уровне БД — миграция 0009.

ТЗ §2 п.6, §6.3, §7, §9.1 п.3, п.5, §9.2; ФТ-12…ФТ-14; DECISIONS.md АР-13, АР-23, АР-31, АР-35,
АР-36; BACKLOG Б-53, Б-54, Б-63, Б-65.

Данные заводит суперпользователь (`engine`) в обход RLS; функции приложения вызываются под
ролью входа `feelings_api` (`user_scope` → `feelings_app`), функции воркера — под
`feelings_worker` (`SET LOCAL ROLE`). Тексты памяти и реплик — произвольные байты (шифрование
здесь не нужно), отпечатки — случайные 32 байта. Реплики и сообщения пары датированы днями
назад, а порог сессии — 2 дня: свежие строки других тестов в кандидаты не попадают."""

import asyncio
import itertools
import os
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.ai.embeddings import to_pgvector
from app.db import user_scope
from app.models.base import EMBEDDING_DIM
from tests.conftest import alembic
from tests.test_couple_functions import SM, _sqlstate

_tg = itertools.count(9_600_000)

GAP = timedelta(days=2)
LEASE = timedelta(hours=1)
DAY = timedelta(days=1)

APP_FUNCS = (
    "app_delete_memory(uuid)",
    "app_memory_forgotten(bytea[])",
    "app_memory_extraction_done(uuid, uuid, uuid)",
    "app_memory_compaction_done(uuid)",
)
WORKER_FUNCS = (
    "memory_claim_extraction(interval, interval, integer)",
    "memory_claim_compaction(interval, interval, integer)",
)
ROLES = ("feelings_app", "feelings_api", "feelings_worker", "feelings_admin", "public")
CLAIM_EXTRACTION = "SELECT * FROM memory_claim_extraction(:g, :l, :n)"
CLAIM_COMPACTION = "SELECT * FROM memory_claim_compaction(:e, :l, :n)"
STATE_COLUMNS = {
    "agents": {
        "memory_partner_through_at",
        "memory_partner_through_id",
        "memory_compacted_at",
        "memory_lease_token",
        "memory_lease_kind",
        "memory_lease_at",
        "memory_lease_until",
        "memory_attempts",
    },
    "conversations": {"memory_through_at", "memory_through_id"},
}


def vec(i: int = 0) -> str:
    v = [0.0] * EMBEDDING_DIM
    v[i] = 1.0
    return to_pgvector(v)


def fp() -> bytes:
    return os.urandom(32)


@dataclass
class P:
    uid: uuid.UUID
    agent: uuid.UUID
    conv: uuid.UUID
    msgs: list[uuid.UUID] = field(default_factory=list)


async def _person(engine: AsyncEngine) -> P:
    p = P(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    async with engine.begin() as c:
        await c.execute(
            text("INSERT INTO users (id, telegram_id) VALUES (:u, :tg)"),
            {"u": p.uid, "tg": next(_tg)},
        )
        await c.execute(
            text("INSERT INTO agents (id, user_id) VALUES (:a, :u)"), {"a": p.agent, "u": p.uid}
        )
        await c.execute(
            text(
                "INSERT INTO conversations (id, agent_id, started_at) "
                "VALUES (:c, :a, now() - interval '60 days')"
            ),
            {"c": p.conv, "a": p.agent},
        )
    return p


async def _turns(engine: AsyncEngine, p: P, ago: Sequence[timedelta]) -> list[uuid.UUID]:
    """Реплики диалога с заданной давностью (владелец и агент по очереди)."""
    ids = []
    async with engine.begin() as c:
        for n, delta in enumerate(ago):
            mid = uuid.uuid4()
            await c.execute(
                text(
                    "INSERT INTO conversation_messages (id, conversation_id, author, text_enc, "
                    "created_at) VALUES (:i, :c, CAST(:au AS message_author), :t, now() - CAST(:d AS interval))"
                ),
                {"i": mid, "c": p.conv, "au": ("owner", "agent")[n % 2], "t": b"x", "d": delta},
            )
            ids.append(mid)
    p.msgs += ids
    return ids


async def _memory(
    engine: AsyncEngine,
    agent: uuid.UUID,
    *,
    finger: bytes | None = None,
    emb: str | None = None,
    model: str | None = None,
    event: bool = True,
) -> uuid.UUID:
    mid = uuid.uuid4()
    async with engine.begin() as c:
        await c.execute(
            text(
                "INSERT INTO memory_items (id, agent_id, type, source, text_enc, event_date, "
                "confidence, fingerprint, embedding, embedding_model) VALUES (:i, :a, 'date', "
                "'owner_dialog', :t, :ev, 0.7, :f, CAST(:e AS vector), :m)"
            ),
            {
                "i": mid,
                "a": agent,
                "t": b"\x01memory",
                "ev": date(2026, 6, 14) if event else None,
                "f": finger if finger is not None else fp(),
                "e": emb,
                "m": model,
            },
        )
    return mid


async def _row(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as c:
        return (await c.execute(text(sql), params)).one()


async def _app(sm: SM, uid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    async with user_scope(sm, uid) as s:
        res = await s.execute(text(sql), params)
        return list(res) if res.returns_rows else []


async def _app_fails(sm: SM, uid: uuid.UUID | None, sql: str, **params: Any) -> str | None:
    with pytest.raises(DBAPIError) as ei:
        async with user_scope(sm, uid) as s:
            await s.execute(text(sql), params)
    return _sqlstate(ei.value)


async def _worker(conn: AsyncConnection, sql: str, **params: Any) -> list[Any]:
    await conn.execute(text("SET LOCAL ROLE feelings_worker"))
    return list(await conn.execute(text(sql), params))


async def _claim(engine: AsyncEngine, agent: uuid.UUID, *, compaction: bool = False) -> Any:
    """Захватывать, пока в выдаче не окажется агент `agent` или выдача не опустеет. Чужие агенты
    (строки других тестов) остаются в аренде на час — им это не мешает."""
    sql = CLAIM_COMPACTION if compaction else CLAIM_EXTRACTION
    params = {"e": timedelta(days=7)} if compaction else {"g": GAP}
    for _ in range(200):
        async with engine.begin() as c:
            rows = await _worker(c, sql, l=LEASE, n=100, **params)
        mine = [r for r in rows if r.agent_id == agent]
        if mine:
            return mine[0]
        if not rows:
            return None
    raise AssertionError("слишком много кандидатов")


async def _state(engine: AsyncEngine, p: P) -> Any:
    return await _row(
        engine,
        "SELECT a.memory_lease_token AS token, a.memory_lease_kind AS kind, "
        "a.memory_attempts AS attempts, a.memory_compacted_at AS compacted_at, "
        "a.memory_lease_at AS lease_at, a.memory_partner_through_id AS partner_through, "
        "c.memory_through_id AS through FROM agents a JOIN conversations c ON c.agent_id = a.id "
        "WHERE a.id = :a",
        a=p.agent,
    )


async def _done(sm: SM, uid: uuid.UUID, token: Any, through: Any, partner: Any = None) -> bool:
    rows = await _app(
        sm,
        uid,
        "SELECT app_memory_extraction_done(:t, :s, :p)",
        t=token,
        s=through,
        p=partner,
    )
    return bool(rows[0][0])


# --- права, свойства функций --------------------------------------------------------------


async def test_function_privileges_and_properties(engine: AsyncEngine) -> None:
    """Функции владельца — только `feelings_app`, захват задач — только `feelings_worker`;
    все SECURITY DEFINER с `search_path = public, pg_temp`, владелец — не роль приложения;
    триггерная функция — ни у кого. Захват отдаёт только id и отметки, без текстов."""
    async with engine.connect() as conn:

        async def can(role: str, f: str) -> bool:
            row = await conn.execute(
                text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"), {"r": role, "f": f}
            )
            return bool(row.scalar_one())

        for f in APP_FUNCS:
            for role in ROLES:
                assert await can(role, f) is (role == "feelings_app"), (role, f)
        for f in WORKER_FUNCS:
            for role in ROLES:
                assert await can(role, f) is (role == "feelings_worker"), (role, f)
        for role in ROLES:
            assert not await can(role, "clear_stale_embedding()"), role
        for f in APP_FUNCS + WORKER_FUNCS:
            row = (
                await conn.execute(
                    text(
                        "SELECT p.prosecdef, p.proconfig, r.rolname, "
                        "pg_get_function_result(p.oid) AS result FROM pg_proc p "
                        "JOIN pg_roles r ON r.oid = p.proowner "
                        "WHERE p.oid = CAST(:f AS regprocedure)"
                    ),
                    {"f": f},
                )
            ).one()
            assert row.prosecdef is True, f
            assert "search_path=public, pg_temp" in row.proconfig, f
            assert not row.rolname.startswith("feelings_"), f
            assert "enc" not in row.result and "bytea" not in row.result.replace(
                "fingerprint bytea", ""
            ), (f, row.result)


async def test_column_privileges_b63(engine: AsyncEngine) -> None:
    """Б-63: вектор, модель, пометка удаления и состояние задач памяти — не пишутся напрямую;
    остальные колонки — пишутся; DELETE у приложения на памяти остался (слияние дубликатов)."""
    async with engine.connect() as conn:

        async def col(role: str, table: str, column: str, priv: str) -> bool:
            row = await conn.execute(
                text("SELECT has_column_privilege(:r, :t, :c, :p)"),
                {"r": role, "t": table, "c": column, "p": priv},
            )
            return bool(row.scalar_one())

        for priv in ("INSERT", "UPDATE"):
            for column in ("embedding", "embedding_model", "deleted_at"):
                assert not await col("feelings_app", "memory_items", column, priv), (column, priv)
            for column in ("text_enc", "fingerprint", "confidence", "type", "event_date"):
                assert await col("feelings_app", "memory_items", column, priv), (column, priv)
            for table, columns in STATE_COLUMNS.items():
                for column in columns:
                    assert not await col("feelings_app", table, column, priv), (table, column)
            assert await col("feelings_app", "agents", "profile_summary_enc", priv)
            assert await col("feelings_app", "conversations", "summary_enc", priv)
            for column in ("embedding", "embedding_model"):
                assert not await col("feelings_admin", "kb_chunks", column, priv), column
            assert await col("feelings_admin", "kb_chunks", "text", priv)
            for table in ("memory_items", "agents", "conversations"):
                assert not await col("feelings_worker", table, "id", priv), table
        row = await conn.execute(
            text("SELECT has_table_privilege('feelings_app', 'memory_items', 'DELETE')")
        )
        assert row.scalar_one() is True


async def test_app_cannot_write_vector_directly(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Б-63: ни вектор, ни модель, ни пометку удаления приложение не пишет мимо функций —
    42501 при INSERT и UPDATE, в т.ч. копированием вектора из своей же записи."""
    p = await _person(engine)
    item = await _memory(engine, p.agent, emb=vec(1), model="m-b63")
    for sql in (
        "UPDATE memory_items SET embedding = embedding WHERE id = :i",
        "UPDATE memory_items SET embedding = NULL, embedding_model = NULL WHERE id = :i",
        "UPDATE memory_items SET embedding_model = 'other' WHERE id = :i",
        "UPDATE memory_items SET deleted_at = now(), text_enc = NULL WHERE id = :i",
        "INSERT INTO memory_items (agent_id, type, source, text_enc, embedding, embedding_model) "
        "SELECT agent_id, 'fact', 'owner_dialog', 'y', embedding, embedding_model "
        "FROM memory_items WHERE id = :i",
        "INSERT INTO memory_items (agent_id, type, source, text_enc, deleted_at) "
        "SELECT agent_id, 'fact', 'owner_dialog', NULL, now() FROM memory_items WHERE id = :i",
    ):
        assert await _app_fails(sessionmaker, p.uid, sql, i=item) == "42501", sql
    for sql in (
        "UPDATE agents SET memory_lease_token = NULL WHERE id = :i",
        "UPDATE agents SET memory_compacted_at = now() WHERE id = :i",
        "UPDATE conversations SET memory_through_at = NULL, memory_through_id = NULL "
        "WHERE agent_id = :i",
    ):
        assert await _app_fails(sessionmaker, p.uid, sql, i=p.agent) == "42501", sql
    assert await _row(engine, "SELECT embedding_model FROM memory_items WHERE id = :i", i=item) == (
        "m-b63",
    )


async def test_text_change_clears_vector(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Изменение текста записи памяти (приложение) и фрагмента БЗ (админ-панель) в том же
    UPDATE обнуляет вектор и модель: вектор старого текста не остаётся (АР-35). Изменение
    других колонок вектор не трогает; новый вектор — только функцией 0008."""
    p = await _person(engine)
    item = await _memory(engine, p.agent)
    assert await _app(sessionmaker, p.uid, "SELECT app_set_memory_embedding(:i, :v, 'm1')",
                      i=item, v=vec(2)) == [(True,)]  # fmt: skip
    await _app(
        sessionmaker, p.uid, "UPDATE memory_items SET confidence = 0.5 WHERE id = :i", i=item
    )
    assert await _row(engine, "SELECT embedding_model FROM memory_items WHERE id = :i", i=item) == (
        "m1",
    )
    await _app(
        sessionmaker,
        p.uid,
        "UPDATE memory_items SET text_enc = 'new', updated_at = now() WHERE id = :i",
        i=item,
    )
    assert tuple(
        await _row(
            engine,
            "SELECT embedding IS NULL, embedding_model IS NULL FROM memory_items WHERE id = :i",
            i=item,
        )
    ) == (True, True)

    doc, chunk = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as c:
        await c.execute(
            text(
                "INSERT INTO kb_documents (id, title, rights_basis, index_status) "
                "VALUES (:d, 'Выдуманная книга', 'тестовые данные', 'indexed')"
            ),
            {"d": doc},
        )
        await c.execute(
            text("INSERT INTO kb_chunks (id, document_id, text) VALUES (:c, :d, 'старый')"),
            {"c": chunk, "d": doc},
        )
        await _worker(c, "SELECT kb_set_chunk_embedding(:c, :v, 'kb-m')", c=chunk, v=vec(3))
    async with engine.begin() as c:
        await c.execute(text("SET LOCAL ROLE feelings_admin"))
        await c.execute(text("UPDATE kb_chunks SET topic_tags = '{t}' WHERE id = :c"), {"c": chunk})
    assert await _row(engine, "SELECT embedding_model FROM kb_chunks WHERE id = :c", c=chunk) == (
        "kb-m",
    )
    for sql in (
        "UPDATE kb_chunks SET embedding = NULL WHERE id = :c",
        "UPDATE kb_chunks SET embedding_model = NULL WHERE id = :c",
        "INSERT INTO kb_chunks (document_id, text, embedding_model) VALUES (:d, 'x', 'm')",
    ):
        with pytest.raises(DBAPIError) as ei:
            async with engine.begin() as c:
                await c.execute(text("SET LOCAL ROLE feelings_admin"))
                await c.execute(text(sql), {"c": chunk, "d": doc})
        assert _sqlstate(ei.value) == "42501", sql
    async with engine.begin() as c:
        await c.execute(text("SET LOCAL ROLE feelings_admin"))
        await c.execute(text("UPDATE kb_chunks SET text = 'новый' WHERE id = :c"), {"c": chunk})
    assert tuple(
        await _row(
            engine,
            "SELECT embedding IS NULL, embedding_model IS NULL FROM kb_chunks WHERE id = :c",
            c=chunk,
        )
    ) == (True, True)


# --- удаление записи владельцем и отпечатки ---------------------------------------------------


async def test_delete_memory_keeps_fingerprint(sessionmaker: SM, engine: AsyncEngine) -> None:
    """§6.3 п.4, ФТ-13: удаление обнуляет текст, вектор, модель и дату события, ставит
    `deleted_at`, сохраняет отпечаток, тип, источник, уверенность; повтор, чужая и
    несуществующая запись — false (неразличимо), чужая не меняется."""
    anna, igor = await _person(engine), await _person(engine)
    f = fp()
    item = await _memory(engine, anna.agent, finger=f, emb=vec(4), model="m-del")
    foreign = await _memory(engine, igor.agent, finger=fp(), emb=vec(4), model="m-del")
    delete = "SELECT app_delete_memory(:i)"
    assert await _app(sessionmaker, anna.uid, delete, i=foreign) == [(False,)]
    assert await _app(sessionmaker, anna.uid, delete, i=uuid.uuid4()) == [(False,)]
    assert await _app(sessionmaker, anna.uid, delete, i=item) == [(True,)]
    assert await _app(sessionmaker, anna.uid, delete, i=item) == [(False,)]
    row = await _row(
        engine,
        "SELECT text_enc, embedding IS NULL AS no_vec, embedding_model, event_date, "
        "deleted_at IS NOT NULL AS deleted, fingerprint, type::text AS type, "
        "source::text AS source, confidence FROM memory_items WHERE id = :i",
        i=item,
    )
    assert (row.text_enc, row.no_vec, row.embedding_model, row.event_date) == (
        None,
        True,
        None,
        None,
    )
    assert (row.deleted, bytes(row.fingerprint), row.type, row.source, row.confidence) == (
        True,
        f,
        "date",
        "owner_dialog",
        0.7,
    )
    other = await _row(
        engine, "SELECT text_enc IS NOT NULL, deleted_at IS NULL FROM memory_items WHERE id = :i",
        i=foreign,
    )  # fmt: skip
    assert tuple(other) == (True, True)
    for uid in (None, uuid.uuid4()):
        assert await _app_fails(sessionmaker, uid, delete, i=item) == "42501"


async def test_tombstone_cannot_be_changed_restored_or_erased(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Удалённую запись приложение видит (своя), но не изменит, не восстановит и не сотрёт
    физически: отпечаток живёт до удаления аккаунта. Живую запись удалить физически можно
    (слияние дубликатов)."""
    p = await _person(engine)
    f = fp()
    gone, live = await _memory(engine, p.agent, finger=f), await _memory(engine, p.agent)
    await _app(sessionmaker, p.uid, "SELECT app_delete_memory(:i)", i=gone)
    async with user_scope(sessionmaker, p.uid) as s:
        seen = await s.execute(
            text("SELECT fingerprint FROM memory_items WHERE id = :i"), {"i": gone}
        )
        assert bytes(seen.scalar_one()) == f
        for sql in (
            "UPDATE memory_items SET text_enc = 'back' WHERE id = :i",
            "UPDATE memory_items SET fingerprint = NULL WHERE id = :i",
            "DELETE FROM memory_items WHERE id = :i",
        ):
            res = await s.execute(text(sql), {"i": gone})
            assert res.rowcount == 0, sql  # type: ignore[attr-defined]
        res = await s.execute(text("DELETE FROM memory_items WHERE id = :i"), {"i": live})
        assert res.rowcount == 1  # type: ignore[attr-defined]
    assert (
        bytes((await _row(engine, "SELECT fingerprint FROM memory_items WHERE id = :i", i=gone))[0])
        == f
    )


async def test_forgotten_fingerprints_only_own_deleted(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """`app_memory_forgotten`: из переданных — только отпечатки удалённых записей своего агента
    (не живых, не партнёра); NULL и пустой массив — пусто; больше 1000 — 22023; без входа — 42501."""
    anna, igor = await _person(engine), await _person(engine)
    gone_f, live_f, igor_f, unknown = fp(), fp(), fp(), fp()
    gone = await _memory(engine, anna.agent, finger=gone_f)
    await _memory(engine, anna.agent, finger=live_f)
    igor_gone = await _memory(engine, igor.agent, finger=igor_f)
    await _app(sessionmaker, anna.uid, "SELECT app_delete_memory(:i)", i=gone)
    await _app(sessionmaker, igor.uid, "SELECT app_delete_memory(:i)", i=igor_gone)
    sql = "SELECT fingerprint FROM app_memory_forgotten(CAST(:f AS bytea[]))"
    got = await _app(sessionmaker, anna.uid, sql, f=[gone_f, live_f, igor_f, unknown, gone_f])
    assert [bytes(r[0]) for r in got] == [gone_f]
    assert [bytes(r[0]) for r in await _app(sessionmaker, igor.uid, sql, f=[gone_f, igor_f])] == [
        igor_f
    ]
    assert await _app(sessionmaker, anna.uid, sql, f=None) == []
    assert await _app(sessionmaker, anna.uid, sql, f=[]) == []
    assert await _app_fails(sessionmaker, anna.uid, sql, f=[fp() for _ in range(1001)]) == "22023"
    assert await _app_fails(sessionmaker, None, sql, f=[gone_f]) == "42501"


async def test_account_delete_removes_fingerprints(sessionmaker: SM, engine: AsyncEngine) -> None:
    """АР-23: удаление аккаунта удаляет и отпечатки удалённых записей, и состояние задач."""
    p = await _person(engine)
    gone = await _memory(engine, p.agent, finger=fp())
    await _app(sessionmaker, p.uid, "SELECT app_delete_memory(:i)", i=gone)
    await _turns(engine, p, [10 * DAY])
    assert await _claim(engine, p.agent) is not None
    await _app(sessionmaker, p.uid, "SELECT app_delete_account()")
    assert await _row(
        engine, "SELECT count(*) FROM memory_items WHERE agent_id = :a", a=p.agent
    ) == (0,)
    assert await _row(engine, "SELECT count(*) FROM agents WHERE id = :a", a=p.agent) == (0,)


# --- извлечение: сессии, догон, конкурентность ----------------------------------------------


async def test_sessions_extracted_in_order_and_once(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Б-54: сессия — реплики с паузой не больше порога; захват выдаёт первую завершённую
    необработанную сессию (`after` — отметка, `through` — её последняя реплика); после
    `done` — следующую; идущая сессия (последняя реплика моложе порога) не выдаётся."""
    p = await _person(engine)
    s1 = await _turns(engine, p, [30 * DAY, 30 * DAY - timedelta(hours=1), 29 * DAY])
    s2 = await _turns(engine, p, [20 * DAY, 20 * DAY - timedelta(minutes=5)])
    await _turns(engine, p, [1 * DAY])  # идёт: моложе порога 2 дня
    first = await _claim(engine, p.agent)
    assert first is not None
    assert (first.user_id, first.conversation_id, first.attempts) == (p.uid, p.conv, 1)
    assert (first.session_after_id, first.session_through_id) == (None, s1[-1])
    assert first.partner_through_id is None
    assert await _claim(engine, p.agent) is None  # аренда занята
    assert await _done(sessionmaker, p.uid, first.token, first.session_through_id) is True
    state = await _state(engine, p)
    assert (state.token, state.kind, state.attempts, state.through) == (None, None, 0, s1[-1])
    second = await _claim(engine, p.agent)
    assert (second.session_after_id, second.session_through_id) == (s1[-1], s2[-1])
    assert await _done(sessionmaker, p.uid, second.token, second.session_through_id) is True
    assert await _claim(engine, p.agent) is None
    # Сессия завершилась (новая реплика после паузы): выдаётся.
    late = await _turns(engine, p, [timedelta(minutes=1)])
    assert await _claim(engine, p.agent) is None  # последняя сессия ещё идёт
    async with engine.begin() as c:
        await c.execute(
            text(
                "UPDATE conversation_messages SET created_at = now() - interval '5 days' "
                "WHERE id = :i"
            ),
            {"i": late[0]},
        )
    third = await _claim(engine, p.agent)
    assert third is not None and third.session_through_id is not None


async def test_done_rejects_bad_marks_and_rolls_back(sessionmaker: SM, engine: AsyncEngine) -> None:
    """`done` — false (транзакцию откатить): чужой или устаревший токен, токен сжатия, реплика
    не своего диалога, отметка не дальше текущей; при откате запись памяти и отметка не
    сохраняются, аренда остаётся."""
    p, other = await _person(engine), await _person(engine)
    s1 = await _turns(engine, p, [30 * DAY])
    s2 = await _turns(engine, p, [20 * DAY])
    foreign = await _turns(engine, other, [30 * DAY])
    c1 = await _claim(engine, p.agent)
    assert c1.session_through_id == s1[0]
    assert await _done(sessionmaker, p.uid, uuid.uuid4(), s1[0]) is False
    assert await _done(sessionmaker, p.uid, None, s1[0]) is False
    assert await _done(sessionmaker, other.uid, c1.token, foreign[0]) is False  # не его аренда
    assert await _done(sessionmaker, p.uid, c1.token, foreign[0]) is False  # не свой диалог
    assert await _done(sessionmaker, p.uid, c1.token, uuid.uuid4()) is False
    # Откат: запись памяти и отметка в одной транзакции.
    with pytest.raises(RuntimeError):
        async with user_scope(sessionmaker, p.uid) as s:
            await s.execute(
                text(
                    "INSERT INTO memory_items (fingerprint, agent_id, type, source, text_enc) "
                    "VALUES (sha256(convert_to(CAST(gen_random_uuid() AS text), 'UTF8')), "  # Б-68 (A1): отпечаток
                    ":a, 'fact', 'owner_dialog', 'rolled-back')"
                ),
                {"a": p.agent},
            )
            ok = await s.execute(
                text("SELECT app_memory_extraction_done(:t, :s, NULL)"),
                {"t": c1.token, "s": s1[0]},
            )
            assert ok.scalar_one() is True
            raise RuntimeError("сбой после done")
    assert await _row(
        engine, "SELECT count(*) FROM memory_items WHERE agent_id = :a", a=p.agent
    ) == (0,)
    state = await _state(engine, p)
    assert (state.token, state.through) == (c1.token, None)
    assert await _done(sessionmaker, p.uid, c1.token, s1[0]) is True
    c2 = await _claim(engine, p.agent)
    assert await _done(sessionmaker, p.uid, c2.token, s1[0]) is False  # не дальше текущей
    assert await _done(sessionmaker, p.uid, c2.token, s2[0]) is True


async def test_lease_expiry_catch_up_after_restart(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Догон: аренда упавшего воркера истекает — та же сессия выдаётся снова с новым токеном,
    `attempts` растёт; завершение по старому токену — false (работа не задвоится)."""
    p = await _person(engine)
    s1 = await _turns(engine, p, [30 * DAY])
    c1 = await _claim(engine, p.agent)
    async with engine.begin() as c:
        await c.execute(
            text(
                "UPDATE agents SET memory_lease_until = now() - interval '1 second' WHERE id = :a"
            ),
            {"a": p.agent},
        )
    c2 = await _claim(engine, p.agent)
    assert c2 is not None and c2.token != c1.token
    assert (c2.session_through_id, c2.attempts) == (s1[0], 2)
    assert await _done(sessionmaker, p.uid, c1.token, s1[0]) is False
    assert await _done(sessionmaker, p.uid, c2.token, s1[0]) is True
    assert (await _state(engine, p)).attempts == 0
    # Истёкшая, но не перехваченная аренда завершается (работа уже сделана).
    await _turns(engine, p, [20 * DAY])
    c3 = await _claim(engine, p.agent)
    async with engine.begin() as c:
        await c.execute(
            text(
                "UPDATE agents SET memory_lease_until = now() - interval '1 second' WHERE id = :a"
            ),
            {"a": p.agent},
        )
    assert await _done(sessionmaker, p.uid, c3.token, c3.session_through_id) is True


async def test_two_workers_skip_locked_without_waiting(engine: AsyncEngine) -> None:
    """Две копии воркера: пока первая не зафиксировала захват, вторая агента пропускает и не
    ждёт (SKIP LOCKED; `lock_timeout` не срабатывает); после фиксации — аренда занята. Строка
    агента, занятая чатом (`FOR NO KEY UPDATE`), тоже пропускается без ожидания."""
    p = await _person(engine)
    await _turns(engine, p, [30 * DAY])
    async with engine.connect() as c1, engine.connect() as c2:
        await c1.begin()
        rows1 = await _worker(c1, CLAIM_EXTRACTION, g=GAP, l=LEASE, n=100)
        assert p.agent in {r.agent_id for r in rows1}
        await c2.begin()
        await c2.execute(text("SET LOCAL lock_timeout = '1s'"))
        rows2 = await _worker(c2, CLAIM_EXTRACTION, g=GAP, l=LEASE, n=100)
        assert p.agent not in {r.agent_id for r in rows2}
        await c2.rollback()
        await c1.commit()
    assert await _claim(engine, p.agent) is None

    q = await _person(engine)
    await _turns(engine, q, [30 * DAY])
    async with engine.connect() as chat, engine.connect() as w:
        await chat.begin()
        await chat.execute(
            text("SELECT 1 FROM agents WHERE id = :a FOR NO KEY UPDATE"), {"a": q.agent}
        )
        await w.begin()
        await w.execute(text("SET LOCAL lock_timeout = '1s'"))
        rows = await _worker(w, CLAIM_EXTRACTION, g=GAP, l=LEASE, n=100)
        assert q.agent not in {r.agent_id for r in rows}
        await w.rollback()
        await chat.rollback()
    assert await _claim(engine, q.agent) is not None


async def test_partner_messages_watermark(sessionmaker: SM, engine: AsyncEngine) -> None:
    """ФТ-14: доставленные владельцу сообщения пары — своя отметка (delivered_at, id); не
    доставленные, адресованные другому и моложе порога не выдаются; отметка не дальше текущей
    и чужое сообщение — false."""
    anna, igor = await _person(engine), await _person(engine)
    couple = uuid.uuid4()
    msgs: dict[str, uuid.UUID] = {}
    async with engine.begin() as c:
        await c.execute(
            text("INSERT INTO couples (id, status) VALUES (:c, 'active')"), {"c": couple}
        )
        for who, role in ((anna, "initiator"), (igor, "partner")):
            await c.execute(
                text(
                    "INSERT INTO couple_members (couple_id, user_id, role) "
                    "VALUES (:c, :u, CAST(:r AS member_role))"
                ),
                {"c": couple, "u": who.uid, "r": role},
            )
        for name, author, recipient, status, ago in (
            ("d1", igor, anna, "delivered", 10 * DAY),
            ("d2", igor, anna, "read", 9 * DAY),
            ("proc", igor, anna, "processing", None),
            ("held", igor, anna, "withheld", None),
            ("to_igor", anna, igor, "delivered", 8 * DAY),
            ("fresh", igor, anna, "delivered", timedelta(hours=1)),
        ):
            msgs[name] = uuid.uuid4()
            await c.execute(
                text(
                    "INSERT INTO partner_messages (id, couple_id, author_id, recipient_id, "
                    "original_enc, status, sent_at, delivered_at) VALUES (:i, :c, :a, :r, 'x', "
                    "CAST(:st AS partner_message_status), now() - interval '11 days', "
                    "now() - CAST(:ago AS interval))"
                ),
                {
                    "i": msgs[name],
                    "c": couple,
                    "a": author.uid,
                    "r": recipient.uid,
                    "st": status,
                    "ago": ago,
                },
            )
    got = await _claim(engine, anna.agent)
    assert got is not None
    assert (got.session_through_id, got.partner_after_id, got.partner_through_id) == (
        None,
        None,
        msgs["d2"],
    )
    assert await _done(sessionmaker, anna.uid, got.token, None, msgs["to_igor"]) is False
    assert await _done(sessionmaker, anna.uid, got.token, None, msgs["proc"]) is False
    assert await _done(sessionmaker, anna.uid, got.token, None, msgs["d2"]) is True
    assert (await _state(engine, anna)).partner_through == msgs["d2"]
    assert await _claim(engine, anna.agent) is None  # «fresh» моложе порога
    async with engine.begin() as c:
        await c.execute(
            text(
                "UPDATE partner_messages SET delivered_at = now() - interval '3 days' WHERE id = :i"
            ),
            {"i": msgs["fresh"]},
        )
    again = await _claim(engine, anna.agent)
    assert (again.partner_after_id, again.partner_through_id) == (msgs["d2"], msgs["fresh"])
    assert await _done(sessionmaker, anna.uid, again.token, None, msgs["d1"]) is False
    assert await _done(sessionmaker, anna.uid, again.token, None, msgs["fresh"]) is True


async def test_deleted_owner_not_claimed_and_done_fails(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Владелец удалил аккаунт во время извлечения: удаление проходит (ждёт транзакцию
    воркера, взаимоблокировки нет), `done` после удаления — 42501 (Б-44, OwnerGone); помеченный
    `deleted_at` владелец не захватывается."""
    p = await _person(engine)
    s1 = await _turns(engine, p, [30 * DAY])
    got = await _claim(engine, p.agent)
    async with user_scope(sessionmaker, p.uid) as s:
        await s.execute(
            text(
                "INSERT INTO memory_items (fingerprint, agent_id, type, source, text_enc) "
                "VALUES (sha256(convert_to(CAST(gen_random_uuid() AS text), 'UTF8')), "  # Б-68 (A1): отпечаток
                ":a, 'fact', 'owner_dialog', 'x')"
            ),
            {"a": p.agent},
        )
        ok = await s.execute(
            text("SELECT app_memory_extraction_done(:t, :s, NULL)"), {"t": got.token, "s": s1[0]}
        )
        assert ok.scalar_one() is True
        deleting = asyncio.create_task(_app(sessionmaker, p.uid, "SELECT app_delete_account()"))
        await asyncio.sleep(0.5)
        assert not deleting.done()  # ждёт строку агента
    await asyncio.wait_for(deleting, 10)
    assert await _row(engine, "SELECT count(*) FROM agents WHERE id = :a", a=p.agent) == (0,)
    assert (
        await _app_fails(
            sessionmaker, p.uid, "SELECT app_memory_extraction_done(:t, NULL, NULL)", t=got.token
        )
        == "42501"
    )

    q = await _person(engine)
    await _turns(engine, q, [30 * DAY])
    async with engine.begin() as c:
        await c.execute(text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": q.uid})
    assert await _claim(engine, q.agent) is None


async def test_claim_argument_checks(engine: AsyncEngine) -> None:
    """Порог сессии 1 мин…7 дней, аренда (0, 1 день], период сжатия от 1 мин — иначе 22023."""
    for sql, params in (
        (CLAIM_EXTRACTION, {"g": timedelta(seconds=30), "l": LEASE}),
        (CLAIM_EXTRACTION, {"g": timedelta(days=8), "l": LEASE}),
        (CLAIM_EXTRACTION, {"g": None, "l": LEASE}),
        (CLAIM_EXTRACTION, {"g": GAP, "l": timedelta(0)}),
        (CLAIM_EXTRACTION, {"g": GAP, "l": timedelta(days=2)}),
        (CLAIM_COMPACTION, {"e": timedelta(seconds=1), "l": LEASE}),
        (CLAIM_COMPACTION, {"e": timedelta(days=7), "l": None}),
    ):
        with pytest.raises(DBAPIError) as ei:
            async with engine.begin() as c:
                await _worker(c, sql, n=1, **params)
        assert _sqlstate(ei.value) == "22023", params


# --- недельное сжатие -------------------------------------------------------------------------


async def test_compaction_claim_and_exclusive_lease(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Сжатие: агент с памятью без прошлого сжатия — выдаётся; `done` ставит время захвата;
    без изменений памяти после сжатия — не выдаётся, удаление записи владельцем — изменение
    (сводка профиля без удалённого, АР-31). Пока идёт извлечение, сжатие не выдаётся (и
    наоборот); токены видов не взаимозаменяемы."""
    p = await _person(engine)
    item = await _memory(engine, p.agent)
    s1 = await _turns(engine, p, [30 * DAY])
    ext = await _claim(engine, p.agent)
    assert await _claim(engine, p.agent, compaction=True) is None  # аренда извлечения
    assert await _done(sessionmaker, p.uid, ext.token, s1[0]) is True
    comp = await _claim(engine, p.agent, compaction=True)
    assert comp is not None and comp.compacted_at is None and comp.attempts == 1
    await _turns(engine, p, [20 * DAY])
    assert await _claim(engine, p.agent) is None  # аренда сжатия
    assert await _done(sessionmaker, p.uid, comp.token, None) is False  # токен сжатия
    done = "SELECT app_memory_compaction_done(:t)"
    assert await _app(sessionmaker, p.uid, done, t=uuid.uuid4()) == [(False,)]
    assert await _app(sessionmaker, p.uid, done, t=comp.token) == [(True,)]
    st = await _state(engine, p)
    assert (st.token, st.attempts, st.compacted_at) == (None, 0, st.lease_at)
    assert await _app(sessionmaker, p.uid, done, t=comp.token) == [(False,)]
    # Период: сжатие недавно — изменения (не удаление) его не вызывают.
    other = await _memory(engine, p.agent)
    assert await _claim(engine, p.agent, compaction=True) is None
    # АР-37 п.1: удаление владельцем ставит отметку — сжатие выдаётся вне периода.
    await _app(sessionmaker, p.uid, "SELECT app_delete_memory(:i)", i=item)
    flagged = await _row(
        engine, "SELECT memory_compaction_requested_at FROM agents WHERE id = :a", a=p.agent
    )
    assert flagged[0] is not None
    urgent = await _claim(engine, p.agent, compaction=True)
    assert urgent is not None
    # Удаление во время сжатия (отметка новее захвата) — отметка остаётся до следующего.
    await _app(sessionmaker, p.uid, "SELECT app_delete_memory(:i)", i=other)
    assert await _app(sessionmaker, p.uid, done, t=urgent.token) == [(True,)]
    left = await _row(
        engine, "SELECT memory_compaction_requested_at FROM agents WHERE id = :a", a=p.agent
    )
    assert left[0] is not None
    last = await _claim(engine, p.agent, compaction=True)
    assert last is not None
    assert await _app(sessionmaker, p.uid, done, t=last.token) == [(True,)]
    cleared = await _row(
        engine, "SELECT memory_compaction_requested_at FROM agents WHERE id = :a", a=p.agent
    )
    assert cleared[0] is None
    assert await _claim(engine, p.agent, compaction=True) is None
    # Период прошёл: без изменений с прошлого сжатия — не выдаётся; с изменением — выдаётся.
    async with engine.begin() as c:
        await c.execute(
            text("UPDATE agents SET memory_compacted_at = now() - interval '8 days' WHERE id = :a"),
            {"a": p.agent},
        )
        await c.execute(
            text(
                "UPDATE memory_items SET deleted_at = now() - interval '9 days', "
                "created_at = now() - interval '9 days' WHERE agent_id = :a"
            ),
            {"a": p.agent},
        )
    assert await _claim(engine, p.agent, compaction=True) is None  # без изменений с прошлого
    async with engine.begin() as c:
        await c.execute(
            text("UPDATE memory_items SET deleted_at = now() WHERE agent_id = :a"),
            {"a": p.agent},
        )
    again = await _claim(engine, p.agent, compaction=True)
    assert again is not None and again.compacted_at is not None


# --- 0010: раунд 1 ревью 2.4 --------------------------------------------------------------


async def test_partner_messages_only_current_couple(sessionmaker: SM, engine: AsyncEngine) -> None:
    """ФТ-14 (АР-37): сообщения бывшей пары не выдаются захватом и не принимаются `done`;
    сообщения текущей активной пары — выдаются."""
    anna, igor, oleg = await _person(engine), await _person(engine), await _person(engine)
    old, cur = uuid.uuid4(), uuid.uuid4()
    msgs: dict[str, uuid.UUID] = {}
    async with engine.begin() as c:
        await c.execute(
            text("INSERT INTO couples (id, status) VALUES (:o, 'dissolved'), (:n, 'active')"),
            {"o": old, "n": cur},
        )
        for couple, members, active in ((old, (anna, oleg), False), (cur, (anna, igor), True)):
            for who, role in zip(members, ("initiator", "partner"), strict=True):
                await c.execute(
                    text(
                        "INSERT INTO couple_members (couple_id, user_id, role, active) "
                        "VALUES (:c, :u, CAST(:r AS member_role), :ac)"
                    ),
                    {"c": couple, "u": who.uid, "r": role, "ac": active},
                )
        for name, couple, author, ago in (
            ("old", old, oleg, 10 * DAY),
            ("cur", cur, igor, 5 * DAY),
        ):
            msgs[name] = uuid.uuid4()
            await c.execute(
                text(
                    "INSERT INTO partner_messages (id, couple_id, author_id, recipient_id, "
                    "original_enc, status, sent_at, delivered_at) VALUES (:i, :c, :a, :r, 'x', "
                    "'delivered', now() - interval '11 days', now() - CAST(:ago AS interval))"
                ),
                {"i": msgs[name], "c": couple, "a": author.uid, "r": anna.uid, "ago": ago},
            )
    got = await _claim(engine, anna.agent)
    assert got is not None
    assert (got.partner_after_id, got.partner_through_id) == (None, msgs["cur"])
    assert await _done(sessionmaker, anna.uid, got.token, None, msgs["old"]) is False
    assert await _done(sessionmaker, anna.uid, got.token, None, msgs["cur"]) is True
    # Пара распалась: доставленное ей сообщение больше не выдаётся.
    async with engine.begin() as c:
        await c.execute(
            text(
                "UPDATE partner_messages SET delivered_at = now() - interval '3 days' WHERE id = :i"
            ),
            {"i": msgs["old"]},
        )
        await c.execute(text("UPDATE couples SET status = 'dissolved' WHERE id = :c"), {"c": cur})
    assert await _claim(engine, anna.agent) is None


async def test_claim_does_not_lock_ongoing_sessions_on_volume(engine: AsyncEngine) -> None:
    """MINOR-4: 1500 агентов с идущей сессией (первая реплика старше порога, последняя — моложе:
    в 0009 такие агенты блокировались перебором) — захват их строки не
    блокирует (чат не ждёт: `FOR NO KEY UPDATE NOWAIT` из другой транзакции проходит, пока
    захват не зафиксирован) и укладывается в секунды; агент с готовой работой выдаётся."""
    base = 7_000_000_000 + int.from_bytes(os.urandom(3), "big") * 10_000
    async with engine.begin() as c:
        await c.execute(
            text(
                "WITH u AS (INSERT INTO users (id, telegram_id) "
                "SELECT gen_random_uuid(), CAST(:b AS bigint) + g FROM generate_series(1, 1500) g RETURNING id), "
                "a AS (INSERT INTO agents (user_id) SELECT id FROM u RETURNING id), "
                "c AS (INSERT INTO conversations (agent_id) SELECT id FROM a RETURNING id) "
                "INSERT INTO conversation_messages (conversation_id, author, text_enc, created_at) "
                "SELECT c.id, 'owner', 'x', now() - d FROM c CROSS JOIN (VALUES (interval '3 days'), "
                "(interval '47 hours')) v(d)"
            ),
            {"b": base},
        )
        ongoing = [
            r[0]
            for r in await c.execute(
                text(
                    "SELECT a.id FROM agents a JOIN users u ON u.id = a.user_id "
                    "WHERE u.telegram_id > CAST(:b AS bigint) AND u.telegram_id <= CAST(:b AS bigint) + 1500"
                ),
                {"b": base},
            )
        ]
    assert len(ongoing) == 1500
    ready = await _person(engine)
    await _turns(engine, ready, [30 * DAY])
    async with engine.connect() as w, engine.connect() as chat:
        await w.begin()
        started = time.monotonic()
        rows = await _worker(w, CLAIM_EXTRACTION, g=GAP, l=LEASE, n=100)
        elapsed = time.monotonic() - started
        assert ready.agent in {r.agent_id for r in rows}
        assert not set(ongoing) & {r.agent_id for r in rows}
        await chat.begin()
        locked = await chat.execute(
            text(
                "SELECT count(*) FROM (SELECT id FROM agents WHERE id = ANY(:ids) "
                "FOR NO KEY UPDATE NOWAIT) x"
            ),
            {"ids": ongoing},
        )
        assert locked.scalar_one() == 1500
        await chat.rollback()
        await w.rollback()
    assert elapsed < 5, elapsed


async def test_text_requires_fingerprint(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Б-68 (MINOR-7): запись с текстом без отпечатка не сохраняется (23514) — ни приложением,
    ни в обход RLS; без текста отпечаток не обязателен."""
    p = await _person(engine)
    sql = (
        "INSERT INTO memory_items (agent_id, type, source, text_enc) "
        "VALUES (:a, 'fact', 'owner_dialog', 'x')"
    )
    assert await _app_fails(sessionmaker, p.uid, sql, a=p.agent) == "23514"
    with pytest.raises(DBAPIError) as ei:
        async with engine.begin() as c:
            await c.execute(text(sql), {"a": p.agent})
    assert _sqlstate(ei.value) == "23514"
    item = await _memory(engine, p.agent)
    with pytest.raises(DBAPIError) as ei:
        async with engine.begin() as c:
            await c.execute(
                text("UPDATE memory_items SET fingerprint = NULL WHERE id = :i"), {"i": item}
            )
    assert _sqlstate(ei.value) == "23514"
    async with engine.begin() as c:
        await c.execute(
            text(
                "INSERT INTO memory_items (agent_id, type, source, text_enc) "
                "VALUES (:a, 'fact', 'owner_dialog', NULL)"
            ),
            {"a": p.agent},
        )


# --- Б-65 и обратимость ------------------------------------------------------------------------


async def _prosrc(engine: AsyncEngine, f: str) -> str:
    row = await _row(engine, "SELECT prosrc FROM pg_proc WHERE oid = CAST(:f AS regprocedure)", f=f)
    return str(row[0])


async def test_memory_nearest_counts_to_1001(engine: AsyncEngine) -> None:
    """Б-65: `app_memory_nearest` не считает все записи агента — только до 1001."""
    src = await _prosrc(engine, "app_memory_nearest(text, text, integer)")
    assert "LIMIT 1001" in src
    assert "SELECT count(*) INTO v_total FROM public.memory_items" not in src


async def test_downgrade_0008_upgrade_check(
    sessionmaker: SM, engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Downgrade до 0008: функции, триггеры, индексы и колонки 0009 удалены, права и политика
    `memory_owner` — как в 0008, `app_memory_nearest` — с `count(*)`; пометки удаления и
    отпечатки остаются. Повторный upgrade и `alembic check` чисты. Последний тест модуля."""
    p = await _person(engine)
    f = fp()
    gone = await _memory(engine, p.agent, finger=f)
    await _app(sessionmaker, p.uid, "SELECT app_delete_memory(:i)", i=gone)
    alembic("downgrade", "0008")
    await engine.dispose()
    await app_engine.dispose()
    try:
        async with engine.connect() as conn:
            for name in (
                "app_delete_memory",
                "app_memory_forgotten",
                "app_memory_extraction_done",
                "app_memory_compaction_done",
                "memory_claim_extraction",
                "memory_claim_compaction",
                "clear_stale_embedding",
            ):
                n = await conn.execute(
                    text("SELECT count(*) FROM pg_proc WHERE proname = :n"), {"n": name}
                )
                assert n.scalar_one() == 0, name
            cols = await conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND column_name LIKE 'memory\\_%'"
                )
            )
            assert cols.scalar_one() == 0
            idx = await conn.execute(
                text(
                    "SELECT count(*) FROM pg_indexes WHERE indexname IN "
                    "('ix_memory_items_agent_fingerprint', 'ix_partner_messages_recipient_delivered',"
                    " 'ix_conversation_messages_conversation_order')"
                )
            )
            assert idx.scalar_one() == 0
            policies = await conn.execute(
                text("SELECT policyname FROM pg_policies WHERE tablename = 'memory_items'")
            )
            assert {r[0] for r in policies} == {"memory_owner"}
            privs = await conn.execute(
                text(
                    "SELECT has_table_privilege('feelings_app', 'memory_items', 'INSERT'), "
                    "has_table_privilege('feelings_app', 'memory_items', 'UPDATE'), "
                    "has_table_privilege('feelings_app', 'agents', 'UPDATE'), "
                    "has_table_privilege('feelings_app', 'conversations', 'INSERT'), "
                    "has_table_privilege('feelings_admin', 'kb_chunks', 'UPDATE')"
                )
            )
            assert tuple(privs.one()) == (True, True, True, True, True)
            kept = await conn.execute(
                text("SELECT fingerprint, deleted_at IS NOT NULL FROM memory_items WHERE id = :i"),
                {"i": gone},
            )
            row = kept.one()
            assert (bytes(row[0]), row[1]) == (f, True)
        src = await _prosrc(engine, "app_memory_nearest(text, text, integer)")
        assert "LIMIT 1001" not in src and "SELECT count(*) INTO v_total" in src
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    alembic("check")
    assert await _app(sessionmaker, p.uid, "SELECT app_delete_memory(:i)", i=gone) == [(False,)]
    forgotten = await _app(
        sessionmaker, p.uid, "SELECT * FROM app_memory_forgotten(CAST(:f AS bytea[]))", f=[f]
    )
    assert [bytes(r[0]) for r in forgotten] == [f]

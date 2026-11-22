"""2.6 (A1): база знаний на уровне БД — миграция 0011.

ТЗ §6.1 п.4, §6.4, §7, §9.1 п.5, §10; ФТ-15; DECISIONS.md АР-24, АР-35, АР-39; BACKLOG Б-63,
Б-65. Материалы — выдуманные тексты (ограничение карточки 2.6: права на книги не подтверждены).
Загрузка и удаление — под `feelings_admin`, индексация — под `feelings_worker`, поиск — под
`feelings_api` (`user_scope` → `feelings_app`); данные в обход ролей — суперпользователь."""

import hashlib
import os
import time
import uuid
from datetime import timedelta
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

LEASE = timedelta(minutes=10)
MAX = 20 * 1024 * 1024
UPLOAD = "SELECT admin_kb_upload(:t, :a, :ap, :r, :f, :s)"
CLAIM = "SELECT * FROM kb_claim_index(:l, :n)"
ADMIN_FUNCS = ("admin_kb_upload(text, text, text, text, text, bytea)", "admin_kb_delete(uuid)")
WORKER_FUNCS = (
    "kb_claim_index(interval, integer)",
    "kb_read_source(uuid, uuid)",
    "kb_add_chunk(uuid, uuid, integer, text, text[], integer)",
    "kb_finish_index(uuid, uuid)",
    "kb_fail_index(uuid, uuid, text)",
)
ROLES = ("feelings_app", "feelings_api", "feelings_worker", "feelings_admin", "public")


def vec(i: int) -> str:
    v = [0.0] * EMBEDDING_DIM
    v[i] = 1.0
    return to_pgvector(v)


def book() -> bytes:
    """Выдуманный текст с уникальной меткой (каждый тест — свой файл, без дублей)."""
    return f"Выдуманная статья о просьбах без упрёков {uuid.uuid4()}".encode()


async def _as(engine: AsyncEngine, role: str, sql: str, **params: Any) -> list[Any]:
    async with engine.begin() as c:
        await c.execute(text(f"SET LOCAL ROLE {role}"))
        res = await c.execute(text(sql), params)
        return list(res) if res.returns_rows else []


async def _role_fails(engine: AsyncEngine, role: str, sql: str, **params: Any) -> str | None:
    with pytest.raises(DBAPIError) as ei:
        await _as(engine, role, sql, **params)
    return _sqlstate(ei.value)


async def _upload(engine: AsyncEngine, data: bytes | None = None, **kw: Any) -> uuid.UUID:
    params = {"t": "Выдуманная книга", "a": None, "ap": "nvc", "r": "тестовые данные", "f": "txt"}
    params |= kw
    source = data if data is not None else book()
    rows = await _as(engine, "feelings_admin", UPLOAD, s=source, **params)
    return uuid.UUID(str(rows[0][0]))


async def _claim(engine: AsyncEngine, doc: uuid.UUID, lease: timedelta = LEASE) -> Any:
    """Захватывать, пока не попадётся `doc` или выдача не опустеет (чужие — в аренде)."""
    for _ in range(50):
        rows = await _as(engine, "feelings_worker", CLAIM, l=lease, n=20)
        mine = [r for r in rows if r.document_id == doc]
        if mine:
            return mine[0]
        if not rows:
            return None
    raise AssertionError("слишком много кандидатов")


async def _w(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    return (await _as(engine, "feelings_worker", sql, **params))[0][0]


async def _doc(engine: AsyncEngine, doc: uuid.UUID) -> Any:
    async with engine.connect() as c:
        return (
            await c.execute(
                text(
                    "SELECT index_status::text AS status, source_data, source_size, "
                    "source_sha256, source_format, index_error, indexed_at, index_attempts, "
                    "index_lease_token FROM kb_documents WHERE id = :d"
                ),
                {"d": doc},
            )
        ).one_or_none()


async def _chunks(engine: AsyncEngine, doc: uuid.UUID) -> list[Any]:
    async with engine.connect() as c:
        return list(
            await c.execute(
                text("SELECT id, ord, text FROM kb_chunks WHERE document_id = :d ORDER BY ord"),
                {"d": doc},
            )
        )


ADD = "SELECT kb_add_chunk(:d, :t, :o, :x, CAST(:tp AS text[]), :lv)"


async def _add(engine: AsyncEngine, doc: uuid.UUID, token: Any, ord_: int, **kw: Any) -> Any:
    params = {"x": f"фрагмент {ord_}", "tp": ["anger"], "lv": 2} | kw
    return await _w(engine, ADD, d=doc, t=token, o=ord_, **params)


# --- права ----------------------------------------------------------------------------------


async def test_function_and_table_privileges(engine: AsyncEngine) -> None:
    """Загрузка и удаление — только администратор; индексация — только воркер. Администратор
    не пишет фрагменты и векторы и не читает исходник и аренду; приложение не читает исходник;
    воркер читает тексты фрагментов (не векторы) и не пишет таблицы напрямую."""
    async with engine.connect() as conn:

        async def q(sql: str, **p: Any) -> bool:
            return bool((await conn.execute(text(sql), p)).scalar_one())

        def fn(r: str, f: str) -> Any:
            return q("SELECT has_function_privilege(:r, :f, 'EXECUTE')", r=r, f=f)

        def col(r: str, t: str, c: str, p: str) -> Any:
            return q("SELECT has_column_privilege(:r, :t, :c, :p)", r=r, t=t, c=c, p=p)

        def tbl(r: str, t: str, p: str) -> Any:
            return q("SELECT has_table_privilege(:r, :t, :p)", r=r, t=t, p=p)

        for f in ADMIN_FUNCS:
            for role in ROLES:
                assert await fn(role, f) is (role == "feelings_admin"), (role, f)
        for f in WORKER_FUNCS:
            for role in ROLES:
                assert await fn(role, f) is (role == "feelings_worker"), (role, f)
            row = (
                await conn.execute(
                    text(
                        "SELECT p.prosecdef, p.proconfig FROM pg_proc p "
                        "WHERE p.oid = CAST(:f AS regprocedure)"
                    ),
                    {"f": f},
                )
            ).one()
            assert row.prosecdef and "search_path=public, pg_temp" in row.proconfig, f
        for role in ("feelings_admin", "feelings_app", "feelings_worker"):
            for table in ("kb_documents", "kb_chunks"):
                for priv in ("INSERT", "UPDATE", "DELETE"):
                    assert not await tbl(role, table, priv), (role, table, priv)
                    if priv != "DELETE":
                        assert not await q(
                            "SELECT has_any_column_privilege(:r, :t, :p)", r=role, t=table, p=priv
                        ), (role, table, priv)
            for column in ("source_data", "index_lease_token", "index_lease_until"):
                assert not await col(role, "kb_documents", column, "SELECT"), (role, column)
            assert await col(role, "kb_documents", "title", "SELECT"), role
        assert await col("feelings_admin", "kb_documents", "index_error", "SELECT")
        assert await col("feelings_admin", "kb_documents", "source_sha256", "SELECT")
        assert await col("feelings_worker", "kb_chunks", "text", "SELECT")
        assert not await col("feelings_worker", "kb_chunks", "embedding", "SELECT")


# --- загрузка ---------------------------------------------------------------------------------


async def test_upload_stores_source_and_audit(engine: AsyncEngine) -> None:
    """Загрузка: статус uploaded, размер, формат, sha256 считает БД; журнал `kb.upload` без
    текстов и без исполнителя (администратор не пользователь, АР-24)."""
    data = book()
    doc = await _upload(engine, data, t="  Выдуманная  ", a="", ap="gottman", f="txt")
    row = await _doc(engine, doc)
    assert (row.status, bytes(row.source_data), row.source_size, row.source_format) == (
        "uploaded",
        data,
        len(data),
        "txt",
    )
    assert bytes(row.source_sha256) == hashlib.sha256(data).digest()
    async with engine.connect() as c:
        meta = (
            await c.execute(
                text("SELECT title, author, approach FROM kb_documents WHERE id = :d"), {"d": doc}
            )
        ).one()
        audit = (
            await c.execute(
                text(
                    "SELECT actor_id, object_type FROM audit_log "
                    "WHERE action = 'kb.upload' AND object_id = :d"
                ),
                {"d": doc},
            )
        ).all()
    assert tuple(meta) == ("Выдуманная", None, "gottman")
    assert [tuple(a) for a in audit] == [(None, "kb_document")]


@pytest.mark.parametrize(
    "bad",
    [
        {"t": ""},
        {"t": "x" * 301},
        {"a": "x" * 201},
        {"ap": "x" * 101},
        {"r": "  "},
        {"r": None},
        {"f": "exe"},
        {"f": None},
        {"s": b""},
    ],
)
async def test_upload_bad_arguments_22023(engine: AsyncEngine, bad: dict[str, Any]) -> None:
    params = {"t": "Книга", "a": None, "ap": None, "r": "тест", "f": "pdf", "s": book()} | bad
    assert await _role_fails(engine, "feelings_admin", UPLOAD, **params) == "22023"


async def test_upload_size_limit_and_duplicates(engine: AsyncEngine) -> None:
    """Больше 20 МиБ — FK001; тот же файл (sha256) повторно — FK002, пока прежний не `failed`;
    после сбоя — можно загрузить снова."""
    big = b"a" * (MAX + 1)
    params = {"t": "Книга", "a": None, "ap": None, "r": "тест", "f": "txt"}
    assert await _role_fails(engine, "feelings_admin", UPLOAD, s=big, **params) == "FK001"
    exact = os.urandom(16) + b"a" * (MAX - 16)
    doc = await _upload(engine, exact)
    assert (await _doc(engine, doc)).source_size == MAX
    assert await _role_fails(engine, "feelings_admin", UPLOAD, s=exact, **params) == "FK002"
    claim = await _claim(engine, doc)
    assert await _w(engine, "SELECT kb_fail_index(:d, :t, 'too_many_pages')", d=doc, t=claim.token)
    again = await _upload(engine, exact)
    assert again != doc


# --- индексация: захват, аренда, догон ---------------------------------------------------------


async def test_index_lifecycle(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Захват → indexing (попытка 1); исходник — только по своему токену; фрагменты по
    порядку; завершение → indexed, исходник стёрт; поиск приложения находит фрагменты."""
    data = book()
    doc = await _upload(engine, data)
    claim = await _claim(engine, doc)
    assert claim is not None and claim.attempts == 1
    assert (claim.source_format, claim.source_size) == ("txt", len(data))
    assert (await _doc(engine, doc)).status == "indexing"
    read = "SELECT kb_read_source(:d, :t)"
    assert bytes(await _w(engine, read, d=doc, t=claim.token)) == data
    assert await _w(engine, read, d=doc, t=uuid.uuid4()) is None
    assert await _w(engine, read, d=doc, t=None) is None
    ids = [await _add(engine, doc, claim.token, i) for i in range(3)]
    assert all(ids)
    assert await _add(engine, doc, uuid.uuid4(), 9) is None  # чужая аренда
    with pytest.raises(DBAPIError) as ei:
        await _add(engine, doc, claim.token, 1)  # тот же порядковый номер
    assert _sqlstate(ei.value) == "23505"
    for bad in ({"x": " "}, {"lv": 6}, {"tp": [f"t{i}" for i in range(33)]}):
        with pytest.raises(DBAPIError) as ei:
            await _add(engine, doc, claim.token, 5, **bad)
        assert _sqlstate(ei.value) == "22023", bad
    with pytest.raises(DBAPIError) as ei:
        await _add(engine, doc, claim.token, -1)
    assert _sqlstate(ei.value) == "22023"
    model = f"kb-{uuid.uuid4().hex[:8]}"
    for n, chunk in enumerate(ids):
        embed = "SELECT kb_set_chunk_embedding(:c, :v, :m)"
        assert await _w(engine, embed, c=chunk, v=vec(n), m=model) is True
    finish = "SELECT kb_finish_index(:d, :t)"
    assert await _w(engine, finish, d=doc, t=uuid.uuid4()) is False
    assert await _w(engine, finish, d=doc, t=claim.token) is True
    row = await _doc(engine, doc)
    assert (row.status, row.source_data, row.index_lease_token) == ("indexed", None, None)
    assert row.indexed_at is not None and row.index_error is None
    assert [c.ord for c in await _chunks(engine, doc)] == [0, 1, 2]
    assert await _w(engine, read, d=doc, t=claim.token) is None
    assert await _w(engine, finish, d=doc, t=claim.token) is False  # повтор
    assert await _claim(engine, doc) is None
    uid = uuid.uuid4()
    async with engine.begin() as c:
        await c.execute(
            text("INSERT INTO users (id, telegram_id) VALUES (:u, :tg)"),
            {"u": uid, "tg": 7_500_000_000 + int.from_bytes(os.urandom(3), "big")},
        )
    async with user_scope(sessionmaker, uid) as s:
        found = await s.execute(
            text("SELECT id FROM app_kb_nearest(:q, :m, 5, NULL, NULL, NULL)"),
            {"q": vec(1), "m": model},
        )
        assert next(r[0] for r in found) == ids[1]


async def test_finish_without_chunks_and_fail(engine: AsyncEngine) -> None:
    """Без фрагментов завершить нельзя (22023 — пустой текст через kb_fail_index); сбой —
    failed с кодом причины, исходник и фрагменты стёрты; неверный код — 22023."""
    doc = await _upload(engine)
    claim = await _claim(engine, doc)
    with pytest.raises(DBAPIError) as ei:
        await _w(engine, "SELECT kb_finish_index(:d, :t)", d=doc, t=claim.token)
    assert _sqlstate(ei.value) == "22023"
    await _add(engine, doc, claim.token, 0)
    fail = "SELECT kb_fail_index(:d, :t, :r)"
    for reason in ("", "Bad", "has space", "x" * 33, None, "текст"):
        with pytest.raises(DBAPIError) as ei:
            await _w(engine, fail, d=doc, t=claim.token, r=reason)
        assert _sqlstate(ei.value) == "22023", reason
    assert await _w(engine, fail, d=doc, t=uuid.uuid4(), r="zip_bomb") is False
    assert await _w(engine, fail, d=doc, t=claim.token, r="zip_bomb") is True
    row = await _doc(engine, doc)
    assert (row.status, row.index_error, row.source_data) == ("failed", "zip_bomb", None)
    assert await _chunks(engine, doc) == []
    assert await _claim(engine, doc) is None


async def test_lease_expiry_catch_up_and_stale_worker(engine: AsyncEngine) -> None:
    """Догон: воркер упал — аренда истекает, материал выдаётся снова (попытка 2), фрагменты
    прерванной попытки стёрты; устаревший токен ничего не пишет и не завершает."""
    doc = await _upload(engine)
    first = await _claim(engine, doc)
    await _add(engine, doc, first.token, 0)
    assert await _claim(engine, doc) is None  # аренда действует
    async with engine.begin() as c:
        await c.execute(
            text(
                "UPDATE kb_documents SET index_lease_until = now() - interval '1 second' "
                "WHERE id = :d"
            ),
            {"d": doc},
        )
    second = await _claim(engine, doc)
    assert second is not None and second.token != first.token and second.attempts == 2
    assert await _chunks(engine, doc) == []
    assert await _add(engine, doc, first.token, 0) is None
    assert await _w(engine, "SELECT kb_finish_index(:d, :t)", d=doc, t=first.token) is False
    await _add(engine, doc, second.token, 0)
    assert await _w(engine, "SELECT kb_finish_index(:d, :t)", d=doc, t=second.token) is True


async def test_two_workers_skip_locked(engine: AsyncEngine) -> None:
    """Две копии воркера: пока первая не зафиксировала захват, вторая материал пропускает и
    не ждёт; после фиксации — аренда занята."""
    doc = await _upload(engine)
    async with engine.connect() as c1, engine.connect() as c2:
        await c1.begin()
        await c1.execute(text("SET LOCAL ROLE feelings_worker"))
        rows1 = list(await c1.execute(text(CLAIM), {"l": LEASE, "n": 20}))
        assert doc in {r.document_id for r in rows1}
        await c2.begin()
        await c2.execute(text("SET LOCAL ROLE feelings_worker"))
        await c2.execute(text("SET LOCAL lock_timeout = '1s'"))
        rows2 = list(await c2.execute(text(CLAIM), {"l": LEASE, "n": 20}))
        assert doc not in {r.document_id for r in rows2}
        await c2.rollback()
        await c1.commit()
    assert await _claim(engine, doc) is None


async def test_claim_argument_checks(engine: AsyncEngine) -> None:
    for lease in (None, timedelta(0), timedelta(days=2)):
        assert await _role_fails(engine, "feelings_worker", CLAIM, l=lease, n=1) == "22023"


# --- удаление материала -----------------------------------------------------------------------


async def test_admin_delete_cascades_and_stops_indexing(engine: AsyncEngine) -> None:
    """Удаление материала: фрагменты — каскадом, журнал `kb.delete`; повтор и несуществующий —
    false; идущая индексация после удаления ничего не пишет и не завершает."""
    doc = await _upload(engine)
    claim = await _claim(engine, doc)
    await _add(engine, doc, claim.token, 0)
    delete = "SELECT admin_kb_delete(:d)"
    assert await _as(engine, "feelings_admin", delete, d=doc) == [(True,)]
    assert await _as(engine, "feelings_admin", delete, d=doc) == [(False,)]
    assert await _as(engine, "feelings_admin", delete, d=uuid.uuid4()) == [(False,)]
    assert await _doc(engine, doc) is None and await _chunks(engine, doc) == []
    assert await _add(engine, doc, claim.token, 1) is None
    assert await _w(engine, "SELECT kb_finish_index(:d, :t)", d=doc, t=claim.token) is False
    async with engine.connect() as c:
        n = await c.execute(
            text(
                "SELECT count(*) FROM audit_log WHERE action = 'kb.delete' AND object_id = :d "
                "AND actor_id IS NULL"
            ),
            {"d": doc},
        )
        assert n.scalar_one() == 1


# --- Б-65: замер фильтров на объёме -----------------------------------------------------------


async def _bulk_chunks(conn: AsyncConnection, docs: list[uuid.UUID], n: int, model: str) -> None:
    await conn.execute(
        text(
            "INSERT INTO kb_chunks (document_id, ord, text, topic_tags, problem_level, embedding, "
            "embedding_model) SELECT (CAST(:docs AS uuid[]))[1 + (g % 20)], g, 'фрагмент ' || g, "
            "ARRAY['b65-' || (g % 30), 'b65-' || ((g * 7) % 30)]::varchar[], 1 + (g % 5), "
            "(SELECT array_agg(random() - 0.5) FROM generate_series(1, 1024) i "
            "WHERE g > 0)::vector, :m FROM generate_series(1, :n) g"
        ),
        {"docs": docs, "n": n, "m": model},
    )


async def test_kb_nearest_filters_on_volume(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Б-65 (АР-39): 4 тыс. фрагментов (замер на 15–20 тыс. — в АР-39; вставка в HNSW на таком
    объёме — минута, для прогона тестов дорого) — поиск без фильтра (HNSW), по теме, по уровню,
    по теме и уровню и без совпадений отвечает полной выдачей (min(5, подходящих)) и быстро."""
    model = f"b65-{uuid.uuid4().hex[:8]}"
    docs = [uuid.uuid4() for _ in range(20)]
    uid = uuid.uuid4()
    async with engine.begin() as c:
        for d in docs:
            await c.execute(
                text(
                    "INSERT INTO kb_documents (id, title, approach, rights_basis, index_status) "
                    "VALUES (:d, 'Выдуманная', 'nvc', 'тест', 'indexed')"
                ),
                {"d": d},
            )
        await _bulk_chunks(c, docs, 4_000, model)
        await c.execute(
            text("INSERT INTO users (id, telegram_id) VALUES (:u, :tg)"),
            {"u": uid, "tg": 7_600_000_000 + int.from_bytes(os.urandom(3), "big")},
        )
    q = to_pgvector([((i * 37) % 101) / 101 - 0.5 for i in range(EMBEDDING_DIM)])
    try:
        timings: dict[str, float] = {}
        for name, topics, level, expect in (
            ("none", None, None, 5),
            ("topic", ["b65-3"], None, 5),
            ("level", None, 2, 5),
            ("both", ["b65-3"], 4, 5),
            ("nothing", ["b65-none"], None, 0),
        ):
            async with user_scope(sessionmaker, uid) as s:
                started = time.monotonic()
                rows = list(
                    await s.execute(
                        text(
                            "SELECT id FROM app_kb_nearest(:q, :m, 5, 'nvc', "
                            "CAST(:tp AS text[]), :lv)"
                        ),
                        {"q": q, "m": model, "tp": topics, "lv": level},
                    )
                )
                timings[name] = time.monotonic() - started
            assert len(rows) == expect, name
        assert max(timings.values()) < 2, timings
    finally:
        async with engine.begin() as c:
            await c.execute(text("DELETE FROM kb_documents WHERE id = ANY(:d)"), {"d": docs})


# --- обратимость --------------------------------------------------------------------------------


async def test_downgrade_0010_upgrade_check(engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    """Downgrade до 0010: функции, колонки, индексы 0011 удалены, права администратора — как
    раньше (запись материалов по таблице); upgrade и `alembic check` чисты. Последний тест."""
    alembic("downgrade", "0010")
    await engine.dispose()
    await app_engine.dispose()
    try:
        async with engine.connect() as conn:
            for name in (
                "admin_kb_upload",
                "admin_kb_delete",
                "kb_claim_index",
                "kb_read_source",
                "kb_add_chunk",
                "kb_finish_index",
                "kb_fail_index",
            ):
                n = await conn.execute(
                    text("SELECT count(*) FROM pg_proc WHERE proname = :n"), {"n": name}
                )
                assert n.scalar_one() == 0, name
            cols = await conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns WHERE table_schema = "
                    "'public' AND ((table_name = 'kb_documents' AND (column_name LIKE 'source\\_%' "
                    "OR column_name LIKE 'index\\_%' OR column_name = 'indexed_at')) "
                    "OR (table_name = 'kb_chunks' AND column_name = 'ord'))"
                )
            )
            # index_status — колонка 0001, остаётся.
            assert cols.scalar_one() == 1
            privs = await conn.execute(
                text(
                    "SELECT has_table_privilege('feelings_admin', 'kb_documents', 'INSERT'), "
                    "has_table_privilege('feelings_admin', 'kb_chunks', 'DELETE'), "
                    "has_column_privilege('feelings_admin', 'kb_chunks', 'text', 'UPDATE'), "
                    "has_column_privilege('feelings_admin', 'kb_chunks', 'embedding', 'UPDATE'), "
                    "has_table_privilege('feelings_app', 'kb_documents', 'SELECT'), "
                    "has_table_privilege('feelings_worker', 'kb_chunks', 'SELECT')"
                )
            )
            assert tuple(privs.one()) == (True, True, True, False, True, False)
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    alembic("check")
    assert await _upload(engine) is not None

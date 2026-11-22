"""2.0v: поиск по смыслу в БД (миграция 0008) — память агента и база знаний.

ТЗ §6.1 п.3–4, §6.3, §6.4, §7, §9.2 п.1, §10; DECISIONS.md АР-5, АР-35; BACKLOG Б-55.
Данные заводит суперпользователь (`engine`), поиск и запись векторов — под ролью входа
приложения `feelings_api` (`user_scope` → `feelings_app`) или под `feelings_worker`.
Тексты и векторы выдуманные; тексты памяти — произвольные байты (шифрование здесь не нужно)."""

import itertools
import time
import uuid
from collections.abc import Sequence
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

_tg = itertools.count(9_700_000)

MEMORY_NEAREST = "SELECT * FROM app_memory_nearest(:q, :m, :n)"
KB_NEAREST = "SELECT * FROM app_kb_nearest(:q, :m, :n, :ap, CAST(:tp AS text[]), :lv)"
APP_FUNCS = (
    "app_memory_nearest(text, text, integer)",
    "app_set_memory_embedding(uuid, text, text)",
    "app_kb_nearest(text, text, integer, text, text[], integer)",
)
WORKER_FUNCS = ("kb_set_chunk_embedding(uuid, text, text)",)
ROLES = ("feelings_app", "feelings_api", "feelings_worker", "feelings_admin", "public")


def vec(parts: dict[int, float]) -> str:
    """Литерал pgvector размерности 1024 с заданными компонентами (остальные — 0)."""
    v = [0.0] * EMBEDDING_DIM
    for i, x in parts.items():
        v[i] = x
    return to_pgvector(v)


Q = vec({0: 1.0})  # вектор запроса


def model() -> str:
    """Своё имя модели на тест: строки других тестов той же модели не попадают в выдачу."""
    return f"test-model-{uuid.uuid4().hex[:12]}"


async def _user(engine: AsyncEngine, *, agent: bool = True) -> tuple[uuid.UUID, uuid.UUID]:
    uid, aid = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (id, telegram_id) VALUES (:u, :tg)"),
            {"u": uid, "tg": next(_tg)},
        )
        if agent:
            await conn.execute(
                text("INSERT INTO agents (id, user_id) VALUES (:a, :u)"), {"a": aid, "u": uid}
            )
    return uid, aid


async def _memory(
    engine: AsyncEngine,
    agent: uuid.UUID,
    emb: str | None,
    mdl: str | None,
    *,
    body: bytes | None = b"\x01memory",
) -> uuid.UUID:
    mid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO memory_items (fingerprint, id, agent_id, type, source, text_enc, embedding, "
                "embedding_model) VALUES (sha256(convert_to(CAST(gen_random_uuid() AS text), 'UTF8')), "  # Б-68 (A1): отпечаток
                ":i, :a, 'fact', 'owner_dialog', :t, "
                "CAST(:e AS vector), :m)"
            ),
            {"i": mid, "a": agent, "t": body, "e": emb, "m": mdl},
        )
    return mid


async def _nearest(sm: SM, uid: uuid.UUID | None, mdl: str, n: int = 5, q: str = Q) -> list[Any]:
    async with user_scope(sm, uid) as s:
        return list(await s.execute(text(MEMORY_NEAREST), {"q": q, "m": mdl, "n": n}))


async def _fails(sm: SM, uid: uuid.UUID | None, sql: str, params: dict[str, Any]) -> DBAPIError:
    with pytest.raises(DBAPIError) as ei:
        async with user_scope(sm, uid) as s:
            await s.execute(text(sql), params)
    return ei.value


# --- права и свойства функций --------------------------------------------------------------


async def test_function_privileges_by_role(engine: AsyncEngine) -> None:
    """Память и поиск по базе знаний — только `feelings_app`; запись векторов фрагментов —
    только воркер; разбор вектора `embedding_arg` — ни у кого (вызывается изнутри функций)."""
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
            assert not await can(role, "embedding_arg(text, text)"), role
        for f in APP_FUNCS + WORKER_FUNCS:
            row = (
                await conn.execute(
                    text(
                        "SELECT p.prosecdef, p.proconfig, r.rolname FROM pg_proc p "
                        "JOIN pg_roles r ON r.oid = p.proowner "
                        "WHERE p.oid = CAST(:f AS regprocedure)"
                    ),
                    {"f": f},
                )
            ).one()
            assert row.prosecdef is True, f
            assert "search_path=public, pg_temp" in row.proconfig, f
            assert not row.rolname.startswith("feelings_"), f
            name = f.split("(")[0]
            total = (
                await conn.execute(
                    text("SELECT count(*) FROM pg_proc WHERE proname = :n"), {"n": name}
                )
            ).scalar_one()
            assert total == 1, f  # нет перегрузок в обход прав


async def test_app_still_has_no_direct_pgvector_access(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Функции pgvector приложению по-прежнему закрыты (0001): вектор передаётся текстом, а
    `CAST(… AS vector)`, `<=>` и `embedding_arg` под `feelings_app` не работают."""
    uid, aid = await _user(engine)
    mid = await _memory(engine, aid, Q, model())
    for sql, params in (
        ("SELECT CAST(CAST(:v AS text) AS vector)", {"v": Q}),
        ("SELECT embedding <=> embedding FROM memory_items WHERE id = :i", {"i": mid}),
        ("SELECT embedding_arg(:v, 'm')", {"v": Q}),
    ):
        err = await _fails(sessionmaker, uid, sql, params)
        assert "permission denied for function" in str(err.orig), sql


async def test_api_login_role_cannot_call_without_set_role(app_engine: AsyncEngine) -> None:
    """`feelings_api` (NOINHERIT) без `SET ROLE feelings_app` функций не вызывает."""
    for sql in (
        "SELECT * FROM app_memory_nearest(:q, 'm', 5)",
        "SELECT * FROM app_kb_nearest(:q, 'm', 5, NULL, NULL, NULL)",
        "SELECT app_set_memory_embedding(gen_random_uuid(), :q, 'm')",
        "SELECT kb_set_chunk_embedding(gen_random_uuid(), :q, 'm')",
    ):
        with pytest.raises(DBAPIError, match="permission denied for function"):
            async with app_engine.connect() as conn:
                await conn.execute(text(sql), {"q": Q})


# --- поиск по памяти: изоляция ----------------------------------------------------------------


async def test_memory_nearest_returns_only_own_live_current_model(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Чужая запись, удалённая, другой модели, без модели (до 0008), без вектора и без текста
    не возвращаются — только свои живые записи с вектором текущей модели."""
    mdl = model()
    anna, anna_agent = await _user(engine)
    igor, igor_agent = await _user(engine)
    own = await _memory(engine, anna_agent, vec({0: 1.0, 1: 0.1}), mdl)
    await _memory(engine, igor_agent, Q, mdl)  # чужая, ближе всех
    await _memory(engine, anna_agent, Q, model())  # другая модель
    await _memory(engine, anna_agent, Q, None)  # вектор без модели (старая строка)
    await _memory(engine, anna_agent, None, None)  # без вектора
    deleted = await _memory(engine, anna_agent, Q, mdl)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE memory_items SET deleted_at = now(), text_enc = NULL, embedding = NULL, "
                "embedding_model = NULL WHERE id = :i"
            ),
            {"i": deleted},
        )
    rows = await _nearest(sessionmaker, anna, mdl, 10)
    assert [r.id for r in rows] == [own]
    assert rows[0].text_enc == b"\x01memory" and rows[0].type == "fact"
    assert rows[0].source == "owner_dialog" and rows[0].confidence == 1.0
    igor_rows = await _nearest(sessionmaker, igor, mdl, 10)
    assert len(igor_rows) == 1 and igor_rows[0].id != own


async def test_memory_nearest_order_limit_and_distance(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    mdl = model()
    uid, aid = await _user(engine)
    ids = [await _memory(engine, aid, vec({0: 1.0, 1: 0.2 * k}), mdl) for k in (3, 0, 5, 1, 4, 2)]
    by_distance = [ids[i] for i in (1, 3, 5, 0, 4, 2)]
    rows = await _nearest(sessionmaker, uid, mdl, 4)
    assert [r.id for r in rows] == by_distance[:4]
    distances = [r.distance for r in rows]
    assert distances == sorted(distances) and distances[0] == pytest.approx(0.0, abs=1e-6)
    assert [r.id for r in await _nearest(sessionmaker, uid, mdl, 0)] == by_distance[:1]
    assert [r.id for r in await _nearest(sessionmaker, uid, mdl, -7)] == by_distance[:1]
    assert len(await _nearest(sessionmaker, uid, mdl, 10_000)) == 6
    # Равные расстояния — по id (детерминированный порядок).
    twins = [await _memory(engine, aid, vec({0: 2.0, 1: 0.0}), mdl) for _ in range(3)]
    rows = await _nearest(sessionmaker, uid, mdl, 4)
    assert [r.id for r in rows] == sorted([by_distance[0], *twins])


async def test_memory_limit_capped_at_50(sessionmaker: SM, engine: AsyncEngine) -> None:
    mdl = model()
    uid, aid = await _user(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO memory_items (fingerprint, agent_id, type, source, text_enc, embedding, "
                "embedding_model) SELECT sha256(convert_to(CAST(gen_random_uuid() AS text), 'UTF8')), "  # Б-68 (A1): отпечаток
                ":a, 'fact', 'owner_dialog', '\\x01', "
                "CAST(:e AS vector), :m FROM generate_series(1, 60)"
            ),
            {"a": aid, "e": Q, "m": mdl},
        )
    assert len(await _nearest(sessionmaker, uid, mdl, 1000)) == 50


async def test_memory_nearest_refuses_anonymous_unknown_and_deleted_user(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    uid, _ = await _user(engine)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": uid})
    for who in (None, uuid.uuid4(), uid):
        for sql, params in (
            (MEMORY_NEAREST, {"q": Q, "m": "m", "n": 5}),
            (KB_NEAREST, {"q": Q, "m": "m", "n": 5, "ap": None, "tp": None, "lv": None}),
            ("SELECT app_set_memory_embedding(gen_random_uuid(), :q, 'm')", {"q": Q}),
        ):
            assert _sqlstate(await _fails(sessionmaker, who, sql, params)) == "42501", (who, sql)


async def test_user_without_agent_gets_empty_result(sessionmaker: SM, engine: AsyncEngine) -> None:
    uid, _ = await _user(engine, agent=False)
    assert await _nearest(sessionmaker, uid, model()) == []


@pytest.mark.parametrize(
    ("q", "mdl"),
    [
        (None, "m"),
        ("[1,2,3]", "m"),  # не та размерность
        ("не вектор", "m"),
        (vec({}), "m"),  # нулевой: косинус не определён
        ("[" + ",".join(["NaN"] * EMBEDDING_DIM) + "]", "m"),
        (Q, None),
        (Q, ""),
        (Q, "x" * 129),
    ],
)
async def test_bad_vector_or_model_is_22023_without_value(
    sessionmaker: SM, engine: AsyncEngine, q: str | None, mdl: str | None
) -> None:
    """Неверный вектор или модель — 22023; в тексте ошибки нет значения вектора."""
    uid, aid = await _user(engine)
    mid = await _memory(engine, aid, None, None)
    for sql, params in (
        (MEMORY_NEAREST, {"q": q, "m": mdl, "n": 5}),
        (KB_NEAREST, {"q": q, "m": mdl, "n": 5, "ap": None, "tp": None, "lv": None}),
        ("SELECT app_set_memory_embedding(:i, :q, :m)", {"i": mid, "q": q, "m": mdl}),
    ):
        err = await _fails(sessionmaker, uid, sql, params)
        assert _sqlstate(err) == "22023", sql
        message = str(err.orig)
        assert "[1,2,3]" not in message and "не вектор" not in message and "NaN" not in message


# --- запись вектора памяти ----------------------------------------------------------------


async def _set_memory(sm: SM, uid: uuid.UUID, item: uuid.UUID, v: str, mdl: str) -> bool:
    async with user_scope(sm, uid) as s:
        return bool(
            (
                await s.execute(
                    text("SELECT app_set_memory_embedding(:i, :v, :m)"),
                    {"i": item, "v": v, "m": mdl},
                )
            ).scalar_one()
        )


async def _stored(engine: AsyncEngine, item: uuid.UUID) -> tuple[str | None, str | None]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT embedding::text AS e, embedding_model FROM memory_items WHERE id = :i"
                ),
                {"i": item},
            )
        ).one()
    return row.e, row.embedding_model


async def test_set_memory_embedding_only_own_live_item(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    mdl = model()
    anna, anna_agent = await _user(engine)
    igor, igor_agent = await _user(engine)
    own = await _memory(engine, anna_agent, None, None)
    foreign = await _memory(engine, igor_agent, None, None)
    no_text = await _memory(engine, anna_agent, None, None, body=None)
    deleted = await _memory(engine, anna_agent, None, None, body=None)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE memory_items SET deleted_at = now() WHERE id = :i"), {"i": deleted}
        )

    assert await _set_memory(sessionmaker, anna, own, Q, mdl) is True
    stored, stored_model = await _stored(engine, own)
    assert stored is not None and stored.startswith("[1,0,0") and stored_model == mdl
    assert [r.id for r in await _nearest(sessionmaker, anna, mdl)] == [own]

    for item in (foreign, no_text, deleted, uuid.uuid4()):
        assert await _set_memory(sessionmaker, anna, item, Q, mdl) is False, item
    for item in (foreign, no_text, deleted):
        assert await _stored(engine, item) == (None, None), item
    assert await _nearest(sessionmaker, igor, mdl) == []

    # Пересчёт другой моделью заменяет вектор и модель; по старой модели запись больше не ищется.
    new_model = model()
    assert await _set_memory(sessionmaker, anna, own, vec({1: 1.0}), new_model) is True
    assert await _nearest(sessionmaker, anna, mdl) == []
    assert [r.id for r in await _nearest(sessionmaker, anna, new_model)] == [own]


async def test_delete_clears_vector_and_model(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Удалённая запись без вектора (CHECK 0001) и без модели (CHECK 0008): иначе 23514. С 0009
    приложение удаляет только функцией ``app_delete_memory`` (Б-63: прямой UPDATE пометки
    удаления, вектора и модели — 42501), а смена текста сама обнуляет вектор и модель."""
    mdl = model()
    uid, aid = await _user(engine)
    item = await _memory(engine, aid, Q, mdl)
    no_text = await _memory(engine, aid, Q, mdl, body=None)
    for assignments, target in (
        ("deleted_at = now()", no_text),  # вектор и модель остались
        ("deleted_at = now(), embedding_model = NULL", no_text),  # вектор остался
        ("deleted_at = now(), embedding = NULL", no_text),  # модель осталась
        ("embedding = NULL", item),  # модель без вектора и у живой записи
        ("embedding_model = ''", item),
    ):
        with pytest.raises(DBAPIError) as ei:
            async with engine.begin() as conn:
                await conn.execute(
                    text(f"UPDATE memory_items SET {assignments} WHERE id = :i"), {"i": target}
                )
        assert _sqlstate(ei.value) == "23514", assignments
    for assignments in (
        "deleted_at = now(), text_enc = NULL, embedding = NULL, embedding_model = NULL",
        "embedding = NULL, embedding_model = NULL",
        "deleted_at = now()",
    ):
        err = await _fails(
            sessionmaker, uid, f"UPDATE memory_items SET {assignments} WHERE id = :i", {"i": item}
        )
        assert _sqlstate(err) == "42501", assignments
    async with user_scope(sessionmaker, uid) as s:
        done = await s.execute(text("SELECT app_delete_memory(:i)"), {"i": item})
        assert done.scalar_one() is True
    assert await _stored(engine, item) == (None, None)
    assert await _nearest(sessionmaker, uid, mdl) == []
    assert await _set_memory(sessionmaker, uid, item, Q, mdl) is False


# --- база знаний ----------------------------------------------------------------------------


async def _kb_document(
    conn: AsyncConnection, approach: str | None, status: str = "indexed"
) -> uuid.UUID:
    did = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO kb_documents (id, title, approach, rights_basis, index_status) "
            "VALUES (:d, 'Выдуманная книга', :ap, 'тестовые данные', "
            "CAST(:st AS kb_index_status))"
        ),
        {"d": did, "ap": approach, "st": status},
    )
    return did


async def _kb_chunk(
    conn: AsyncConnection,
    doc: uuid.UUID,
    emb: str | None,
    mdl: str | None,
    tags: Sequence[str] = (),
    level: int | None = None,
) -> uuid.UUID:
    cid = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO kb_chunks (id, document_id, text, topic_tags, problem_level, embedding, "
            "embedding_model) VALUES (:c, :d, :t, CAST(:tg AS varchar[]), :lv, "
            "CAST(:e AS vector), :m)"
        ),
        {
            "c": cid,
            "d": doc,
            "t": f"фрагмент {cid}",
            "tg": list(tags),
            "lv": level,
            "e": emb,
            "m": mdl,
        },
    )
    return cid


async def _kb(
    sm: SM,
    uid: uuid.UUID,
    mdl: str,
    n: int = 5,
    approach: str | None = None,
    topics: list[str] | None = None,
    level: int | None = None,
) -> list[Any]:
    async with user_scope(sm, uid) as s:
        return list(
            await s.execute(
                text(KB_NEAREST),
                {"q": Q, "m": mdl, "n": n, "ap": approach, "tp": topics, "lv": level},
            )
        )


async def test_kb_nearest_filters_by_labels_and_status(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    mdl = model()
    uid, _ = await _user(engine)
    async with engine.begin() as conn:
        nvc = await _kb_document(conn, "nvc")
        gottman = await _kb_document(conn, "gottman")
        draft = await _kb_document(conn, "nvc", "indexing")
        a = await _kb_chunk(conn, nvc, vec({0: 1.0, 1: 0.1}), mdl, ["money", "trust"], 2)
        b = await _kb_chunk(conn, nvc, vec({0: 1.0, 1: 0.3}), mdl, ["anger"], 3)
        c = await _kb_chunk(conn, gottman, vec({0: 1.0, 1: 0.2}), mdl, ["money"], 3)
        await _kb_chunk(conn, gottman, Q, model(), ["money"], 3)  # другая модель
        await _kb_chunk(conn, gottman, Q, None, ["money"], 3)  # без модели
        await _kb_chunk(conn, gottman, None, None, ["money"], 3)  # без вектора
        await _kb_chunk(conn, draft, Q, mdl, ["money"], 2)  # материал не проиндексирован

    rows = await _kb(sessionmaker, uid, mdl)
    assert [r.id for r in rows] == [a, c, b]
    assert rows[0].text == f"фрагмент {a}" and rows[0].document_id == nvc
    assert rows[0].approach == "nvc" and rows[0].topic_tags == ["money", "trust"]
    assert rows[0].problem_level == 2
    assert [r.distance for r in rows] == sorted(r.distance for r in rows)
    assert [r.id for r in await _kb(sessionmaker, uid, mdl, approach="nvc")] == [a, b]
    assert [r.id for r in await _kb(sessionmaker, uid, mdl, topics=["money"])] == [a, c]
    assert [r.id for r in await _kb(sessionmaker, uid, mdl, topics=["anger", "trust"])] == [a, b]
    assert [r.id for r in await _kb(sessionmaker, uid, mdl, topics=[])] == [a, c, b]
    assert [r.id for r in await _kb(sessionmaker, uid, mdl, level=3)] == [c, b]
    assert [
        r.id
        for r in await _kb(sessionmaker, uid, mdl, approach="gottman", topics=["money"], level=3)
    ] == [c]
    assert await _kb(sessionmaker, uid, mdl, approach="attachment") == []
    assert [r.id for r in await _kb(sessionmaker, uid, mdl, n=2)] == [a, c]
    assert [r.id for r in await _kb(sessionmaker, uid, mdl, n=0)] == [a]
    for bad in (0, 6, -1):
        err = await _fails(
            sessionmaker,
            uid,
            KB_NEAREST,
            {"q": Q, "m": mdl, "n": 5, "ap": None, "tp": None, "lv": bad},
        )
        assert _sqlstate(err) == "22023", bad


async def test_kb_limit_capped_at_20(sessionmaker: SM, engine: AsyncEngine) -> None:
    mdl = model()
    uid, _ = await _user(engine)
    async with engine.begin() as conn:
        doc = await _kb_document(conn, "nvc")
        for k in range(25):
            await _kb_chunk(conn, doc, vec({0: 1.0, 1: 0.01 * k}), mdl)
    assert len(await _kb(sessionmaker, uid, mdl, n=100)) == 20


async def test_kb_vectors_written_only_by_worker(sessionmaker: SM, engine: AsyncEngine) -> None:
    mdl = model()
    uid, _ = await _user(engine)
    async with engine.begin() as conn:
        doc = await _kb_document(conn, "nvc")
        chunk = await _kb_chunk(conn, doc, None, None)

    async def as_role(role: str, sql: str, params: dict[str, Any]) -> Any:
        async with engine.connect() as conn, conn.begin():
            await conn.execute(text(f"SET LOCAL ROLE {role}"))
            return (await conn.execute(text(sql), params)).scalar_one()

    put = "SELECT kb_set_chunk_embedding(:c, :v, :m)"
    assert await as_role("feelings_worker", put, {"c": chunk, "v": Q, "m": mdl}) is True
    assert await as_role("feelings_worker", put, {"c": uuid.uuid4(), "v": Q, "m": mdl}) is False
    assert [r.id for r in await _kb(sessionmaker, uid, mdl)] == [chunk]
    with pytest.raises(DBAPIError) as ei:
        await as_role("feelings_worker", put, {"c": chunk, "v": "[1,2]", "m": mdl})
    assert _sqlstate(ei.value) == "22023"
    # Приложение, админ-панель и воркер не выходят за своё назначение.
    for role, sql in (
        ("feelings_app", put),
        ("feelings_admin", put),
        ("feelings_worker", "SELECT count(*) FROM app_kb_nearest(:v, :m, 5, NULL, NULL, NULL)"),
        ("feelings_worker", "SELECT count(*) FROM app_memory_nearest(:v, :m, 5)"),
        ("feelings_admin", "SELECT count(*) FROM app_kb_nearest(:v, :m, 5, NULL, NULL, NULL)"),
    ):
        with pytest.raises(DBAPIError, match="permission denied for function"):
            await as_role(role, sql, {"c": chunk, "v": Q, "m": mdl})
    # Напрямую записать вектор админ-панель не может: приведение к vector(1024) закрыто.
    with pytest.raises(DBAPIError, match="permission denied for function"):
        await as_role(
            "feelings_admin",
            "WITH u AS (UPDATE kb_chunks SET embedding = CAST(:v AS text)::vector "
            "WHERE id = :c RETURNING 1) SELECT count(*) FROM u",
            {"c": chunk, "v": Q},
        )


# --- объём ~10 тыс. строк: индекс HNSW и добор точным перебором ------------------------------

RANDOM_VECTOR = (
    "(SELECT array_agg(CASE WHEN i = 1 THEN CAST(:head AS float8) * (0.5 + random()) "
    "ELSE random() - 0.5 END "
    "+ g * 0) FROM generate_series(1, 1024) i)::vector"
)


async def _numscans(conn: AsyncConnection, index: str) -> int:
    return int(
        (
            await conn.execute(
                text("SELECT pg_stat_get_xact_numscans(CAST(:i AS regclass))"), {"i": index}
            )
        ).scalar_one()
    )


async def _bulk(conn: AsyncConnection, agent: uuid.UUID, n: int, head: float, mdl: str) -> None:
    await conn.execute(
        text(
            "INSERT INTO memory_items (fingerprint, agent_id, type, source, text_enc, embedding, "
            f"embedding_model) SELECT sha256(convert_to(CAST(gen_random_uuid() AS text), 'UTF8')), "  # Б-68 (A1): отпечаток
            f":a, 'fact', 'owner_dialog', '\\x01', {RANDOM_VECTOR}, :m "
            "FROM generate_series(1, :n) g"
        ),
        {"a": agent, "n": n, "head": head, "m": mdl},
    )


async def _exact_memory(conn: AsyncConnection, agent: uuid.UUID, mdl: str, n: int) -> list[Any]:
    await conn.execute(text("SET LOCAL enable_indexscan = off"))
    rows = await conn.execute(
        text(
            "SELECT id FROM memory_items WHERE agent_id = :a AND embedding_model = :m "
            "ORDER BY embedding <=> CAST(:q AS vector), id LIMIT :n"
        ),
        {"a": agent, "m": mdl, "q": Q, "n": n},
    )
    await conn.execute(text("SET LOCAL enable_indexscan = on"))
    return [r.id for r in rows]


async def _as_user(conn: AsyncConnection, uid: uuid.UUID, sql: str, params: dict[str, Any]) -> Any:
    """Вызов под `feelings_app` в той же транзакции (данные объёмного теста не фиксируются)."""
    await conn.execute(text("SET LOCAL ROLE feelings_app"))
    await conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(uid)})
    started = time.perf_counter()
    rows = list(await conn.execute(text(sql), params))
    elapsed = time.perf_counter() - started
    await conn.execute(text("RESET ROLE"))
    return rows, elapsed


async def test_memory_search_on_10k_rows_uses_hnsw_and_falls_back(engine: AsyncEngine) -> None:
    """10 тыс. записей одного агента: поиск идёт по HNSW (счётчик сканов индекса в транзакции и
    EXPLAIN), быстро и только по своим. Агент с 1500 записями вдали от запроса (фильтр по
    агенту после обхода графа ничего не оставляет) и агент с 5 записями получают точный ответ.
    Всё в одной откатываемой транзакции: индекс строится заново после вставки (так быстрее)."""
    mdl = model()
    big, big_agent = uuid.uuid4(), uuid.uuid4()
    far, far_agent = uuid.uuid4(), uuid.uuid4()
    small, small_agent = uuid.uuid4(), uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        for uid, aid in ((big, big_agent), (far, far_agent), (small, small_agent)):
            await conn.execute(
                text("INSERT INTO users (id, telegram_id) VALUES (:u, :tg)"),
                {"u": uid, "tg": next(_tg)},
            )
            await conn.execute(
                text("INSERT INTO agents (id, user_id) VALUES (:a, :u)"), {"a": aid, "u": uid}
            )
        await conn.execute(text("DROP INDEX ix_memory_items_embedding_hnsw"))
        await _bulk(conn, big_agent, 10_000, 30.0, mdl)  # рядом с запросом
        await _bulk(conn, far_agent, 1_500, -30.0, mdl)  # напротив запроса
        await _bulk(conn, small_agent, 5, -30.0, mdl)
        await conn.execute(text("SET LOCAL maintenance_work_mem = '256MB'"))
        await conn.execute(
            text(
                "CREATE INDEX ix_memory_items_embedding_hnsw ON memory_items "
                "USING hnsw (embedding vector_cosine_ops)"
            )
        )
        await conn.execute(text("ANALYZE memory_items"))

        plan = "\n".join(
            r[0]
            for r in await conn.execute(
                text(
                    "EXPLAIN SELECT m.id FROM memory_items m WHERE m.agent_id = :a "
                    "AND m.deleted_at IS NULL AND m.text_enc IS NOT NULL "
                    "AND m.embedding IS NOT NULL AND m.embedding_model = :m "
                    "ORDER BY m.embedding <=> CAST(:q AS vector) LIMIT 5"
                ),
                {"a": big_agent, "m": mdl, "q": Q},
            )
        )
        assert "ix_memory_items_embedding_hnsw" in plan, plan

        params = {"q": Q, "m": mdl, "n": 5}
        before = await _numscans(conn, "ix_memory_items_embedding_hnsw")
        rows, elapsed = await _as_user(conn, big, MEMORY_NEAREST, params)
        assert await _numscans(conn, "ix_memory_items_embedding_hnsw") == before + 1
        assert elapsed < 1.0, elapsed
        assert len(rows) == 5
        assert [r.distance for r in rows] == sorted(r.distance for r in rows)
        exact = await _exact_memory(conn, big_agent, mdl, 50)
        assert {r.id for r in rows} <= set(exact)  # HNSW приближённый, но из своих ближайших

        # 1500 записей вдали от запроса: планировщик сам может выбрать индекс agent_id; с
        # `enable_sort = off` он берёт HNSW — обход графа находит только записи большого агента,
        # фильтр их отбрасывает, и функция добирает точным перебором. 5 записей — сразу перебор.
        for (uid, aid), force_hnsw in (
            ((far, far_agent), False),
            ((far, far_agent), True),
            ((small, small_agent), False),
        ):
            await conn.execute(text(f"SET LOCAL enable_sort = {'off' if force_hnsw else 'on'}"))
            before = await _numscans(conn, "ix_memory_items_embedding_hnsw")
            rows, elapsed = await _as_user(conn, uid, MEMORY_NEAREST, params)
            scans = await _numscans(conn, "ix_memory_items_embedding_hnsw") - before
            await conn.execute(text("SET LOCAL enable_sort = on"))
            if force_hnsw:
                assert scans == 1
            if aid == small_agent:
                assert scans == 0
            assert elapsed < 1.0, elapsed
            exact = await _exact_memory(conn, aid, mdl, 5)
            assert [r.id for r in rows] == exact, (uid, force_hnsw)
        await conn.rollback()


async def test_kb_search_on_10k_chunks_uses_hnsw_and_falls_back(engine: AsyncEngine) -> None:
    """10 тыс. фрагментов: без фильтров — HNSW; редкая метка у далёких фрагментов (фильтр после
    обхода графа ничего не оставляет) — точный перебор с тем же результатом, что эталон."""
    mdl = model()
    uid = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO users (id, telegram_id) VALUES (:u, :tg)"),
            {"u": uid, "tg": next(_tg)},
        )
        doc = await _kb_document(conn, "nvc")
        await conn.execute(text("DROP INDEX ix_kb_chunks_embedding_hnsw"))
        for head, tag, n in ((30.0, "common", 10_000), (-30.0, "rare", 30)):
            await conn.execute(
                text(
                    "INSERT INTO kb_chunks (document_id, text, topic_tags, problem_level, "
                    f"embedding, embedding_model) SELECT :d, 'фрагмент', ARRAY[:tag], 1 + g % 5, "
                    f"{RANDOM_VECTOR}, :m FROM generate_series(1, :n) g"
                ),
                {"d": doc, "tag": tag, "n": n, "head": head, "m": mdl},
            )
        await conn.execute(text("SET LOCAL maintenance_work_mem = '256MB'"))
        await conn.execute(
            text(
                "CREATE INDEX ix_kb_chunks_embedding_hnsw ON kb_chunks "
                "USING hnsw (embedding vector_cosine_ops)"
            )
        )
        await conn.execute(text("ANALYZE kb_chunks"))
        base = {"q": Q, "m": mdl, "n": 5, "ap": None, "lv": None}

        before = await _numscans(conn, "ix_kb_chunks_embedding_hnsw")
        rows, elapsed = await _as_user(conn, uid, KB_NEAREST, {**base, "tp": None})
        assert await _numscans(conn, "ix_kb_chunks_embedding_hnsw") == before + 1
        assert elapsed < 1.0, elapsed
        assert len(rows) == 5 and all(r.topic_tags == ["common"] for r in rows)

        # С `enable_sort = off` планировщик берёт HNSW и для редкой метки: обход графа находит
        # только «common», фильтр их отбрасывает — функция добирает точным перебором.
        await conn.execute(text("SET LOCAL enable_sort = off"))
        before = await _numscans(conn, "ix_kb_chunks_embedding_hnsw")
        rows, elapsed = await _as_user(conn, uid, KB_NEAREST, {**base, "tp": ["rare"]})
        assert await _numscans(conn, "ix_kb_chunks_embedding_hnsw") == before + 1
        await conn.execute(text("SET LOCAL enable_sort = on"))
        assert elapsed < 1.0, elapsed
        await conn.execute(text("SET LOCAL enable_indexscan = off"))
        exact = [
            r.id
            for r in await conn.execute(
                text(
                    "SELECT id FROM kb_chunks WHERE embedding_model = :m AND topic_tags && "
                    "ARRAY['rare']::varchar[] ORDER BY embedding <=> CAST(:q AS vector), id "
                    "LIMIT 5"
                ),
                {"m": mdl, "q": Q},
            )
        ]
        assert [r.id for r in rows] == exact and len(exact) == 5
        await conn.rollback()


# --- обратимость ------------------------------------------------------------------------------


async def test_downgrade_0007_upgrade_check(
    sessionmaker: SM, engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Downgrade до 0007 убирает функции, индексы и колонки модели, векторы остаются; повторный
    upgrade и `alembic check` чисты. Последний тест модуля: схема пересоздаётся."""
    mdl = model()
    uid, aid = await _user(engine)
    item = await _memory(engine, aid, Q, mdl)
    alembic("downgrade", "0007")
    await engine.dispose()
    await app_engine.dispose()
    try:
        async with engine.connect() as conn:
            columns = {
                r[0]
                for r in await conn.execute(
                    text(
                        "SELECT table_name || '.' || column_name FROM information_schema.columns "
                        "WHERE column_name = 'embedding_model'"
                    )
                )
            }
            assert columns == set()
            for name in (
                "app_memory_nearest",
                "app_set_memory_embedding",
                "app_kb_nearest",
                "kb_set_chunk_embedding",
                "embedding_arg",
            ):
                count = await conn.execute(
                    text("SELECT count(*) FROM pg_proc WHERE proname = :n"), {"n": name}
                )
                assert count.scalar_one() == 0, name
            indexes = await conn.execute(
                text("SELECT count(*) FROM pg_indexes WHERE indexname LIKE '%embedding_hnsw'")
            )
            assert indexes.scalar_one() == 0
            kept = await conn.execute(
                text("SELECT embedding IS NOT NULL FROM memory_items WHERE id = :i"), {"i": item}
            )
            assert kept.scalar_one() is True
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    alembic("check")
    # После повторного upgrade старый вектор без модели не ищется, пока его не пересчитают.
    assert await _nearest(sessionmaker, uid, mdl) == []
    assert await _set_memory(sessionmaker, uid, item, Q, mdl) is True
    assert [r.id for r in await _nearest(sessionmaker, uid, mdl)] == [item]

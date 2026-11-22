"""2.0v, A8: независимая проверка векторного поиска в БД (миграция 0008, АР-35), перевода слоя
доступа (A3) и публичного `/help/contacts` (A2).

Данные заводит суперпользователь; проверки — под ролями, как в приложении: `feelings_app` через
`user_scope`, `feelings_api` без `SET ROLE`, `feelings_worker`/`feelings_admin` — `SET LOCAL ROLE`.
Эталон выдачи — точный перебор тем же оператором `<=>` суперпользователем (индексы выключены).
Хелперы исполнителя (`test_vector_search_db.py`, `test_chat_round1.py`) не используются.
Данные выдуманные. Дефекты — `xfail(strict=True)` (docs/reviews/2.0v-test.md)."""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from app.ai.context import build_context
from app.ai.embeddings import EmbeddingError, HashingEmbedder, to_pgvector
from app.config import Settings, get_settings
from app.core import crypto
from app.db import user_scope
from app.help import help_contacts
from app.main import app
from app.repositories import chat as chat_repo
from app.repositories import knowledge as kb_repo
from tests.conftest import BACKEND_DIR, TEST_DATABASE_URL
from tests.test_chat_a8 import KEYS

SM = async_sessionmaker[AsyncSession]
DIM = 1024
MODEL = "a8-model"
OTHER = "a8-other-model"
_rng = random.Random(20261122)  # noqa: S311 — не криптография


def rvec(rng: random.Random = _rng) -> list[float]:
    return [rng.gauss(0, 1) for _ in range(DIM)]


def near(base: Sequence[float], eps: float, rng: random.Random = _rng) -> list[float]:
    return [x + rng.gauss(0, eps) for x in base]


def lit(v: Sequence[float]) -> str:
    return to_pgvector(v)


# =============================================================== данные


async def person(c: AsyncConnection, name: str = "A8") -> tuple[uuid.UUID, uuid.UUID]:
    uid, aid = uuid.uuid4(), uuid.uuid4()
    await c.execute(
        text(
            "INSERT INTO users (id, telegram_id, name, consent_terms_at, consent_data_at, "
            "consent_sensitive_at, age_confirmed_at) VALUES (:i, :t, :n, now(), now(), now(), now())"
        ),
        {"i": uid, "t": 730_000_000_000 + _rng.randrange(10**10), "n": name},
    )
    await c.execute(text("INSERT INTO agents (id, user_id) VALUES (:a, :u)"), {"a": aid, "u": uid})
    return uid, aid


async def mem(
    c: AsyncConnection,
    agent: uuid.UUID,
    vec: Sequence[float] | None,
    *,
    model: str | None = MODEL,
    body: str | None = "x",
    deleted: bool = False,
    when: datetime | None = None,
) -> uuid.UUID:
    mid = uuid.uuid4()
    await c.execute(
        text(
            "INSERT INTO memory_items (id, agent_id, type, text_enc, source, embedding, "
            "embedding_model, created_at, deleted_at) VALUES (:i, :a, 'fact', :t, 'owner_dialog', "
            "CAST(:v AS vector), :m, :w, :d)"
        ),
        {
            "i": mid,
            "a": agent,
            "t": None if body is None else KEYS.seal(crypto.MEMORY_TEXT, mid, body),
            "v": None if vec is None else lit(vec),
            "m": model if vec is not None else None,
            "w": when or datetime.now(UTC),
            "d": datetime.now(UTC) if deleted else None,
        },
    )
    return mid


async def bulk_mem(
    c: AsyncConnection, agent: uuid.UUID, vecs: Sequence[Sequence[float]], model: str = MODEL
) -> list[uuid.UUID]:
    ids = [uuid.uuid4() for _ in vecs]
    await c.execute(
        text(
            "INSERT INTO memory_items (id, agent_id, type, text_enc, source, embedding, "
            "embedding_model) VALUES (:i, :a, 'fact', :t, 'owner_dialog', CAST(:v AS vector), :m)"
        ),
        [
            {"i": i, "a": agent, "t": b"enc", "v": lit(v), "m": model}
            for i, v in zip(ids, vecs, strict=True)
        ],
    )
    return ids


async def exact_memory(
    engine: AsyncEngine, agent: uuid.UUID, q: Sequence[float], model: str, limit: int
) -> list[uuid.UUID]:
    """Эталон: точный перебор (индексы выключены) по тем же условиям, что обещает АР-35."""
    async with engine.begin() as c:
        await c.execute(text("SET LOCAL enable_indexscan = off"))
        await c.execute(text("SET LOCAL enable_bitmapscan = off"))
        rows = await c.execute(
            text(
                "SELECT id FROM memory_items WHERE agent_id = :a AND deleted_at IS NULL "
                "AND text_enc IS NOT NULL AND embedding IS NOT NULL AND embedding_model = :m "
                "ORDER BY embedding <=> CAST(:q AS vector), id LIMIT :n"
            ),
            {"a": agent, "m": model, "q": lit(q), "n": limit},
        )
        return [r.id for r in rows]


async def nearest(
    sm: SM, uid: uuid.UUID, q: Sequence[float] | str | None, model: str | None, limit: Any
) -> list[Any]:
    async with user_scope(sm, uid) as s:
        rows = await s.execute(
            text("SELECT id, distance FROM app_memory_nearest(:q, :m, :n)"),
            {"q": q if q is None or isinstance(q, str) else lit(q), "m": model, "n": limit},
        )
        return list(rows)


# =============================================================== 1. Права и свойства функций

APP_FUNCS = (
    "app_memory_nearest(text, text, integer)",
    "app_set_memory_embedding(uuid, text, text)",
    "app_kb_nearest(text, text, integer, text, text[], integer)",
)
WORKER_FUNCS = ("kb_set_chunk_embedding(uuid, text, text)",)
HELPER = "embedding_arg(text, text)"
ROLES = ("feelings_app", "feelings_worker", "feelings_admin", "feelings_api", "public")


async def test_function_privileges_and_properties(engine: AsyncEngine) -> None:
    """АР-35: `app_*` — только `feelings_app`; `kb_set_chunk_embedding` — только `feelings_worker`;
    `embedding_arg` — никому (и не definer); у `feelings_api` без `SET ROLE` и у PUBLIC — ничего;
    definer-функции с `search_path = public, pg_temp`; HNSW `vector_cosine_ops` на обеих таблицах;
    CHECK `*_model_has_vector`."""
    expected = {f: {"feelings_app"} for f in APP_FUNCS} | {
        f: {"feelings_worker"} for f in WORKER_FUNCS
    }
    expected[HELPER] = set()
    async with engine.connect() as c:
        for fn, allowed in expected.items():
            for role in ROLES:
                if role == "public":
                    got = (
                        await c.execute(
                            text(
                                "SELECT bool_or(acl.grantee = 0 AND acl.privilege_type = 'EXECUTE') "
                                "FROM pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', "
                                "p.proowner))) acl WHERE p.oid = CAST(:f AS regprocedure)"
                            ),
                            {"f": fn},
                        )
                    ).scalar_one()
                else:
                    got = (
                        await c.execute(
                            text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                            {"r": role, "f": fn},
                        )
                    ).scalar_one()
                assert bool(got) == (role in allowed), (fn, role)
            props = (
                await c.execute(
                    text(
                        "SELECT prosecdef, proconfig, r.rolcanlogin, r.rolname FROM pg_proc p "
                        "JOIN pg_roles r ON r.oid = p.proowner WHERE p.oid = CAST(:f AS regprocedure)"
                    ),
                    {"f": fn},
                )
            ).one()
            assert props.prosecdef == (fn != HELPER), fn
            assert any(
                x.replace('"', "").replace(" ", "") == "search_path=public,pg_temp"
                for x in props.proconfig or []
            ), (fn, props.proconfig)
        idx = (
            await c.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE indexname IN "
                    "('ix_memory_items_embedding_hnsw', 'ix_kb_chunks_embedding_hnsw')"
                )
            )
        ).all()
        assert len(idx) == 2 and all("hnsw" in d and "vector_cosine_ops" in d for _, d in idx)
        checks = {
            r[0]
            for r in await c.execute(
                text(
                    "SELECT conname FROM pg_constraint WHERE conname IN "
                    "('ck_memory_items_model_has_vector', 'ck_kb_chunks_model_has_vector')"
                )
            )
        }
        assert len(checks) == 2


async def test_api_role_without_set_role_cannot_call(app_engine: AsyncEngine) -> None:
    """Роль входа `feelings_api` (NOINHERIT) без `SET ROLE` функций не вызывает; под
    `feelings_app` прямой pgvector закрыт (`::vector`, `<=>`)."""
    q = lit(rvec())
    for sql in (
        "SELECT * FROM app_memory_nearest(:q, 'm', 1)",
        "SELECT app_set_memory_embedding(gen_random_uuid(), :q, 'm')",
        "SELECT * FROM app_kb_nearest(:q, 'm', 1, NULL, NULL, NULL)",
        "SELECT kb_set_chunk_embedding(gen_random_uuid(), :q, 'm')",
        "SELECT embedding_arg(:q, 'm')",
    ):
        async with app_engine.connect() as c:
            with pytest.raises(DBAPIError, match="permission denied"):
                await c.execute(text(sql), {"q": q})
    for sql in (
        "SELECT CAST(CAST(:q AS text) AS vector)",
        "SELECT CAST(:q AS vector) <=> CAST(:q AS vector)",
        "SELECT embedding_arg(:q, 'm')",
        "SELECT kb_set_chunk_embedding(gen_random_uuid(), :q, 'm')",
    ):
        async with app_engine.begin() as c:
            await c.execute(text("SET LOCAL ROLE feelings_app"))
            with pytest.raises(DBAPIError, match="permission denied"):
                await c.execute(text(sql), {"q": q})


async def _as(engine: AsyncEngine, role: str, sql: str, **params: Any) -> Any:
    async with engine.begin() as c:
        await c.execute(text(f"SET LOCAL ROLE {role}"))
        return (await c.execute(text(sql), params)).all()


async def test_worker_and_admin_roles(engine: AsyncEngine) -> None:
    """Воркер — только запись вектора фрагмента; поиск ему недоступен; admin — ни поиска, ни
    записи вектора (и напрямую `CAST(... AS vector)` не может)."""
    q = lit(rvec())
    with pytest.raises(DBAPIError, match="permission denied"):
        await _as(engine, "feelings_worker", "SELECT * FROM app_memory_nearest(:q, 'm', 1)", q=q)
    for sql in (
        "SELECT * FROM app_kb_nearest(:q, 'm', 1, NULL, NULL, NULL)",
        "SELECT kb_set_chunk_embedding(gen_random_uuid(), :q, 'm')",
        "UPDATE kb_chunks SET embedding = CAST(:q AS vector) WHERE false",
    ):
        with pytest.raises(DBAPIError, match="permission denied"):
            await _as(engine, "feelings_admin", sql, q=q)
    assert await _as(engine, "feelings_worker",
                     "SELECT kb_set_chunk_embedding(gen_random_uuid(), :q, 'm')", q=q) == [(False,)]  # fmt: skip


# =============================================================== 2. Изоляция памяти, малый объём


@dataclass
class MemWorld:
    uid: uuid.UUID
    agent: uuid.UUID
    other_uid: uuid.UUID
    q: list[float]
    good: list[uuid.UUID]
    bad: dict[str, uuid.UUID]


@pytest.fixture
async def mw(engine: AsyncEngine) -> MemWorld:
    q = rvec()
    async with engine.begin() as c:
        uid, agent = await person(c, "Анна")
        other_uid, other = await person(c, "Игорь")
        good = [await mem(c, agent, near(q, 0.3 * (i + 1))) for i in range(12)]
        bad = {
            "foreign": await mem(c, other, near(q, 0.01)),
            "other_model": await mem(c, agent, near(q, 0.01), model=OTHER),
            "no_model": await mem(c, agent, near(q, 0.01), model=None),
            "no_vector": await mem(c, agent, None),
            "no_text": await mem(c, agent, near(q, 0.01), body=None),
            "deleted": await mem(c, agent, None, body=None, deleted=True),
        }
    return MemWorld(uid, agent, other_uid, q, good, bad)


async def test_memory_isolation_and_exact_order(
    mw: MemWorld, sessionmaker: SM, engine: AsyncEngine
) -> None:
    """ТЗ §6.1 п.3, §9.2 п.1, АР-5, АР-35; кр.1 карточки: своя живая память с текстом и вектором
    текущей модели; чужая, другой модели, без модели, без вектора, без текста, удалённая — нет.
    Выдача совпадает с точным перебором по составу и порядку; расстояние — косинусное."""
    for limit in (1, 5, 8, 12, 50):
        got = await nearest(sessionmaker, mw.uid, mw.q, MODEL, limit)
        ids = [r.id for r in got]
        assert ids == await exact_memory(engine, mw.agent, mw.q, MODEL, limit)
        assert not set(ids) & set(mw.bad.values())
        assert [r.distance for r in got] == sorted(r.distance for r in got)
    # Другая модель находит только свою запись; партнёр не видит память Анны.
    assert [r.id for r in await nearest(sessionmaker, mw.uid, mw.q, OTHER, 10)] == [
        mw.bad["other_model"]
    ]
    partner = [r.id for r in await nearest(sessionmaker, mw.other_uid, mw.q, MODEL, 50)]
    assert partner == [mw.bad["foreign"]]


async def test_memory_limits_and_ties(mw: MemWorld, sessionmaker: SM, engine: AsyncEngine) -> None:
    """АР-35: предел памяти 1–50; NULL и ≤ 0 → 1; равные расстояния — по `id`."""
    for lim, want in ((None, 1), (0, 1), (-7, 1), (3, 3), (51, 12), (10**6, 12)):
        assert len(await nearest(sessionmaker, mw.uid, mw.q, MODEL, lim)) == want, lim
    async with engine.begin() as c:
        same = [await mem(c, mw.agent, mw.q) for _ in range(4)]
    got = [r.id for r in await nearest(sessionmaker, mw.uid, mw.q, MODEL, 4)]
    assert got == sorted(same)


BAD_VECTORS = {
    "null": None,
    "empty": "",
    "garbage": "abc",
    "short": "[0.5,0.25]",
    "long": "[" + ",".join(["0.123456789"] * (DIM + 1)) + "]",
    "nan": "[" + ",".join(["NaN"] + ["0.123456789"] * (DIM - 1)) + "]",
    "inf": "[" + ",".join(["Infinity"] + ["0.123456789"] * (DIM - 1)) + "]",
    "zero": "[" + ",".join(["0"] * DIM) + "]",
    "overflow": "[" + ",".join(["1e39"] + ["0.123456789"] * (DIM - 1)) + "]",
    "nested": "[[0.123456789]]",
}
GOOD = "[" + ",".join(["0.123456789"] * DIM) + "]"
BAD_MODELS = {"null": None, "empty": "", "long": "m" * 129}


@pytest.mark.parametrize("vec", list(BAD_VECTORS))
async def test_bad_vectors_22023_without_value(
    vec: str, mw: MemWorld, sessionmaker: SM, engine: AsyncEngine
) -> None:
    """АР-35: каждый плохой вектор — 22023 во всех функциях; в тексте ошибки нет значения
    вектора (оно производное от личного текста)."""
    v = BAD_VECTORS[vec]
    calls = [
        ("SELECT * FROM app_memory_nearest(:q, :m, 5)", "app"),
        ("SELECT app_set_memory_embedding(:i, :q, :m)", "app"),
        ("SELECT * FROM app_kb_nearest(:q, :m, 5, NULL, NULL, NULL)", "app"),
        ("SELECT kb_set_chunk_embedding(gen_random_uuid(), :q, :m)", "worker"),
    ]
    for sql, who in calls:
        params = {"q": v, "m": MODEL, "i": mw.good[0]}
        with pytest.raises(DBAPIError) as e:
            if who == "app":
                async with user_scope(sessionmaker, mw.uid) as s:
                    await s.execute(text(sql), params)
            else:
                await _as(engine, "feelings_worker", sql, **params)
        assert getattr(e.value.orig, "sqlstate", None) == "22023", (sql, str(e.value.orig))
        msg = str(e.value.orig)
        assert "0.123456789" not in msg and "1e39" not in msg and "NaN" not in msg, msg


@pytest.mark.parametrize("model", list(BAD_MODELS))
async def test_bad_models_22023(model: str, mw: MemWorld, sessionmaker: SM) -> None:
    """АР-35: модель NULL, пустая, длиннее 128 — 22023 (поиск и запись)."""
    for sql in (
        "SELECT * FROM app_memory_nearest(:q, :m, 5)",
        "SELECT app_set_memory_embedding(:i, :q, :m)",
        "SELECT * FROM app_kb_nearest(:q, :m, 5, NULL, NULL, NULL)",
    ):
        with pytest.raises(DBAPIError) as e:
            async with user_scope(sessionmaker, mw.uid) as s:
                await s.execute(text(sql), {"q": GOOD, "m": BAD_MODELS[model], "i": mw.good[0]})
        assert e.value.orig.sqlstate == "22023"  # type: ignore[union-attr]


async def test_no_user_or_deleted_user_42501(
    mw: MemWorld, sessionmaker: SM, engine: AsyncEngine
) -> None:
    """АР-35: без `app.user_id`, неизвестный и помеченный удалённым пользователь — 42501;
    пользователь без агента — пусто."""
    async with engine.begin() as c:
        await c.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": mw.other_uid}
        )
        lonely = uuid.uuid4()
        await c.execute(
            text("INSERT INTO users (id, telegram_id) VALUES (:i, :t)"),
            {"i": lonely, "t": 740_000_000_000 + _rng.randrange(10**9)},
        )
    for uid in (None, uuid.uuid4(), mw.other_uid):
        for sql in (
            "SELECT * FROM app_memory_nearest(:q, 'm', 5)",
            "SELECT app_set_memory_embedding(gen_random_uuid(), :q, 'm')",
            "SELECT * FROM app_kb_nearest(:q, 'm', 5, NULL, NULL, NULL)",
        ):
            with pytest.raises(DBAPIError) as e:
                async with user_scope(sessionmaker, uid) as s:
                    await s.execute(text(sql), {"q": GOOD})
            assert e.value.orig.sqlstate == "42501", (uid, sql)  # type: ignore[union-attr]
    assert await nearest(sessionmaker, lonely, GOOD, MODEL, 5) == []


# =============================================================== 3. Большой объём, путь HNSW


async def _hnsw_scans(engine: AsyncEngine) -> int:
    async with engine.connect() as c:
        await c.execute(text("SELECT pg_stat_clear_snapshot()"))
        return int(
            (
                await c.execute(
                    text(
                        "SELECT idx_scan FROM pg_stat_user_indexes "
                        "WHERE indexrelname = 'ix_memory_items_embedding_hnsw'"
                    )
                )
            ).scalar_one()
        )


async def _call_and_flush(
    engine: AsyncEngine, uid: uuid.UUID, q: Sequence[float], limit: int
) -> list[Any]:
    """Вызов под `feelings_app` (как `user_scope`) на соединении суперпользователя — чтобы сразу
    сбросить статистику (`pg_stat_force_next_flush` — только суперпользователю)."""
    async with engine.begin() as c:
        await c.execute(text("SET LOCAL ROLE feelings_app"))
        await c.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(uid)})
        rows = list(
            await c.execute(
                text("SELECT id, distance FROM app_memory_nearest(:q, :m, :n)"),
                {"q": lit(q), "m": MODEL, "n": limit},
            )
        )
        await c.execute(text("RESET ROLE"))
        await c.execute(text("SELECT pg_stat_force_next_flush()"))
    return rows


async def test_large_volume_hnsw_and_fallback(sessionmaker: SM, engine: AsyncEngine) -> None:
    """АР-35, ТЗ §10: > 1000 записей агента — путь HNSW (индекс используется, вызов < 1 с), выдача
    полная, упорядочена, изоляция и фильтры те же; recall@8 против точного перебора ≥ 0,9.
    Агент с > 1000 записями среди многократно большего числа чужих — добор точным перебором:
    выдача совпадает с точной."""
    rng = random.Random(7)  # noqa: S311
    centers = [rvec(rng) for _ in range(30)]

    def clustered(n: int) -> list[list[float]]:
        return [near(centers[rng.randrange(30)], 0.6, rng) for _ in range(n)]

    async with engine.begin() as c:
        big_uid, big = await person(c, "Много")
        await bulk_mem(c, big, clustered(1300))
        await bulk_mem(c, big, clustered(50), model=OTHER)
        small_uid, small = await person(c, "Среди чужих")
        await bulk_mem(c, small, clustered(1050))
        crowd_uid, crowd = await person(c, "Толпа")
        await bulk_mem(c, crowd, clustered(7000))
        await c.execute(text("ANALYZE memory_items"))

    async def check(uid: uuid.UUID, agent: uuid.UUID) -> float:
        recalls = []
        for _ in range(10):
            q = near(centers[rng.randrange(30)], 0.6, rng)
            t0 = asyncio.get_running_loop().time()
            got = await _call_and_flush(engine, uid, q, 8)
            assert asyncio.get_running_loop().time() - t0 < 1.0
            exact = await exact_memory(engine, agent, q, MODEL, 8)
            ids = [r.id for r in got]
            assert len(ids) == 8 and len(set(ids)) == 8
            assert [r.distance for r in got] == sorted(r.distance for r in got)
            recalls.append(len(set(ids) & set(exact)) / 8)
            async with engine.connect() as c:
                owners = await c.execute(
                    text(
                        "SELECT DISTINCT agent_id, embedding_model FROM memory_items "
                        "WHERE id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": ids},
                )
                assert owners.all() == [(agent, MODEL)]  # изоляция на пути > 1000
        return sum(recalls) / len(recalls)

    before = await _hnsw_scans(engine)
    crowd_recall = await check(crowd_uid, crowd)  # 7000 записей агента — индекс HNSW
    await asyncio.sleep(0.6)
    after = await _hnsw_scans(engine)
    assert after > before, "индекс HNSW не использовался"
    assert crowd_recall >= 0.9, crowd_recall
    # 1300 записей среди 9400: планировщик вправе выбрать перебор по agent_id — выдача полная.
    assert await check(big_uid, big) >= 0.9
    for _ in range(5):
        q = near(centers[rng.randrange(30)], 0.6, rng)
        got = [r.id for r in await nearest(sessionmaker, small_uid, q, MODEL, 8)]
        assert got == await exact_memory(engine, small, q, MODEL, 8)
    assert len(await nearest(sessionmaker, crowd_uid, centers[0], MODEL, 60)) == 50  # предел 50
    # Изоляция по модели на пути HNSW: 50 записей другой модели находит только та модель.
    assert len(await nearest(sessionmaker, big_uid, centers[0], OTHER, 50)) == 50


# =============================================================== 4. Запись вектора


async def test_set_memory_embedding_own_live_only(
    mw: MemWorld, sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Кр.2: вектор пишется только в свою живую запись с текстом (чужая, удалённая, без текста,
    несуществующая — false неразличимо); модель пишется, `updated_at` не меняется; пересчёт другой
    моделью переносит запись в поиск той модели."""
    v = rvec()

    async def set_(uid: uuid.UUID, item: uuid.UUID, model: str = MODEL) -> bool:
        async with user_scope(sessionmaker, uid) as s:
            return bool(
                (
                    await s.execute(
                        text("SELECT app_set_memory_embedding(:i, :v, :m)"),
                        {"i": item, "v": lit(v), "m": model},
                    )
                ).scalar_one()
            )

    async with engine.connect() as c:
        before = (
            await c.execute(
                text("SELECT updated_at FROM memory_items WHERE id = :i"),
                {"i": mw.bad["no_vector"]},
            )
        ).scalar_one()
    assert await set_(mw.uid, mw.bad["no_vector"]) is True
    for item in (mw.bad["foreign"], mw.bad["deleted"], mw.bad["no_text"], uuid.uuid4()):
        assert await set_(mw.uid, item) is False, item
    assert await set_(mw.other_uid, mw.good[0]) is False  # партнёр — в чужую
    async with engine.connect() as c:
        row = (
            await c.execute(
                text("SELECT embedding_model, updated_at FROM memory_items WHERE id = :i"),
                {"i": mw.bad["no_vector"]},
            )
        ).one()
        assert row.embedding_model == MODEL and row.updated_at == before
        foreign = (
            await c.execute(
                text("SELECT embedding_model FROM memory_items WHERE id = :i"),
                {"i": mw.bad["foreign"]},
            )
        ).scalar_one()
        assert foreign == MODEL  # не тронута
    assert [r.id for r in await nearest(sessionmaker, mw.uid, v, MODEL, 1)] == [mw.bad["no_vector"]]
    assert await set_(mw.uid, mw.good[0], OTHER) is True
    assert mw.good[0] in [r.id for r in await nearest(sessionmaker, mw.uid, v, OTHER, 5)]
    assert mw.good[0] not in [r.id for r in await nearest(sessionmaker, mw.uid, mw.q, MODEL, 50)]
    # Слой доступа A3 — то же.
    assert (
        await chat_repo.set_memory_embedding(sessionmaker, mw.uid, mw.bad["foreign"], v, MODEL)
        is False
    )


async def test_delete_check_constraints(mw: MemWorld, engine: AsyncEngine) -> None:
    """Кр.2, CHECK 0001/0008: удаление без обнуления вектора или модели — 23514; полное обнуление
    (текст, вектор, модель) — проходит; модель без вектора не хранится; пустая модель — нельзя.

    С 0009 (Б-63) изменение текста само обнуляет вектор и модель (триггер
    ``clear_stale_embedding``), поэтому CHECK проверяется на строке, где текст не меняется
    (``no_text``: текста уже нет, вектор и модель есть)."""
    item, no_text = mw.good[1], mw.bad["no_text"]
    bad_updates = [
        ("UPDATE memory_items SET deleted_at = now() WHERE id = :i", no_text),
        (
            "UPDATE memory_items SET deleted_at = now(), text_enc = NULL, embedding = NULL "
            "WHERE id = :i",
            no_text,
        ),
        ("UPDATE memory_items SET embedding = NULL WHERE id = :i", item),
        ("UPDATE memory_items SET embedding_model = '' WHERE id = :i", item),
    ]
    for sql, target in bad_updates:
        with pytest.raises(DBAPIError) as e:
            async with engine.begin() as c:
                await c.execute(text(sql), {"i": target})
        assert e.value.orig.sqlstate == "23514", sql  # type: ignore[union-attr]
    async with engine.begin() as c:
        await c.execute(
            text(
                "UPDATE memory_items SET deleted_at = now(), text_enc = NULL, embedding = NULL, "
                "embedding_model = NULL WHERE id = :i"
            ),
            {"i": no_text},
        )
        # Стирание текста без явного обнуления вектора: триггер 0009 обнуляет вектор и модель.
        await c.execute(
            text("UPDATE memory_items SET deleted_at = now(), text_enc = NULL WHERE id = :i"),
            {"i": item},
        )
        left = (
            await c.execute(
                text(
                    "SELECT embedding IS NULL, embedding_model IS NULL FROM memory_items "
                    "WHERE id = :i"
                ),
                {"i": item},
            )
        ).one()
        assert tuple(left) == (True, True)


# =============================================================== 5. База знаний


@dataclass
class KbWorld:
    uid: uuid.UUID
    q: list[float]
    chunks: dict[str, uuid.UUID]


@pytest.fixture
async def kw(engine: AsyncEngine) -> KbWorld:
    q = rvec()
    chunks: dict[str, uuid.UUID] = {}
    async with engine.begin() as c:
        uid, _ = await person(c, "Читатель")
        docs: dict[str, uuid.UUID] = {}
        for name, approach, status in (
            ("cbt", "a8-cbt", "indexed"),
            ("gottman", "a8-gottman", "indexed"),
            ("noappr", None, "indexed"),
            ("uploaded", "a8-cbt", "uploaded"),
            ("indexing", "a8-cbt", "indexing"),
            ("failed", "a8-cbt", "failed"),
        ):
            d = uuid.uuid4()
            docs[name] = d
            await c.execute(
                text(
                    "INSERT INTO kb_documents (id, title, approach, rights_basis, index_status) "
                    "VALUES (:i, :t, :a, 'a8', CAST(:s AS kb_index_status))"
                ),
                {"i": d, "t": name, "a": approach, "s": status},
            )
        spec = [
            ("cbt1", "cbt", ["a8-anger", "a8-money"], 2, MODEL, 0.1),
            ("cbt2", "cbt", ["a8-money"], 3, MODEL, 0.2),
            ("cbt3", "cbt", [], None, MODEL, 0.3),
            ("cbt_other", "cbt", ["a8-money"], 2, OTHER, 0.01),
            ("cbt_novec", "cbt", ["a8-money"], 2, None, 0.0),
            ("got1", "gottman", ["a8-anger"], 2, MODEL, 0.15),
            ("got2", "gottman", ["a8-trust"], 4, MODEL, 0.25),
            ("noappr1", "noappr", ["a8-money"], 2, MODEL, 0.12),
            ("up1", "uploaded", ["a8-money"], 2, MODEL, 0.01),
            ("ing1", "indexing", ["a8-money"], 2, MODEL, 0.01),
            ("fail1", "failed", ["a8-money"], 2, MODEL, 0.01),
        ]
        for name, doc, tags, level, model, eps in spec:
            cid = uuid.uuid4()
            chunks[name] = cid
            await c.execute(
                text(
                    "INSERT INTO kb_chunks (id, document_id, text, topic_tags, problem_level, "
                    "embedding, embedding_model) VALUES (:i, :d, :t, CAST(:tg AS varchar[]), :l, "
                    "CAST(:v AS vector), :m)"
                ),
                {
                    "i": cid,
                    "d": docs[doc],
                    "t": f"TEXT-{name}",
                    "tg": tags,
                    "l": level,
                    "v": None if model is None else lit(near(q, eps * 10)),
                    "m": model,
                },
            )
    return KbWorld(uid, q, chunks)


async def kb(
    sm: SM, w: KbWorld, *, limit: int = 20, approach: str | None = None,
    topics: list[str] | None = None, level: int | None = None, model: str = MODEL,
) -> list[str]:  # fmt: skip
    inv = {v: k for k, v in w.chunks.items()}
    async with user_scope(sm, w.uid) as s:
        rows = await s.execute(
            text("SELECT id, distance FROM app_kb_nearest(:q, :m, :n, :a, CAST(:t AS text[]), :l)"),
            {"q": lit(w.q), "m": model, "n": limit, "a": approach, "t": topics, "l": level},
        )
        out = list(rows)
    assert [r.distance for r in out] == sorted(r.distance for r in out)
    return [inv.get(r.id, "?") for r in out]


async def kb_exact(
    engine: AsyncEngine, w: KbWorld, *, approach: str | None = None,
    topics: list[str] | None = None, level: int | None = None, model: str = MODEL,
) -> list[str]:  # fmt: skip
    """Эталон: точный перебор по условиям АР-35 (только фрагменты мира A8)."""
    inv = {v: k for k, v in w.chunks.items()}
    async with engine.begin() as c:
        await c.execute(text("SET LOCAL enable_indexscan = off"))
        rows = await c.execute(
            text(
                "SELECT c.id FROM kb_chunks c JOIN kb_documents d ON d.id = c.document_id "
                "WHERE d.index_status = 'indexed' AND c.embedding IS NOT NULL "
                "AND c.embedding_model = :m AND c.id = ANY(CAST(:ids AS uuid[])) "
                "AND (CAST(:a AS text) IS NULL OR d.approach = :a) "
                "AND (CAST(:t AS text[]) IS NULL OR cardinality(CAST(:t AS text[])) = 0 "
                "     OR c.topic_tags && CAST(:t AS varchar[])) "
                "AND (CAST(:l AS integer) IS NULL OR c.problem_level = :l) "
                "ORDER BY c.embedding <=> CAST(:q AS vector), c.id"
            ),
            {
                "m": model,
                "ids": list(w.chunks.values()),
                "a": approach,
                "t": topics,
                "l": level,
                "q": lit(w.q),
            },
        )
        return [inv[r.id] for r in rows]


async def test_kb_filters_status_model_limits(
    kw: KbWorld, sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Кр.3, ТЗ §6.4, АР-35: только материалы `indexed`; только векторы модели запроса; подход —
    точно; темы — пересечение (пустой массив/NULL — без фильтра); уровень — точно (без уровня при
    фильтре не попадают); порядок по расстоянию; предел 1–20."""
    a8 = {"cbt1", "cbt2", "cbt3", "got1", "got2", "noappr1"}
    mine = [n for n in await kb(sessionmaker, kw) if n != "?"]
    assert set(mine) == a8
    assert mine == await kb_exact(engine, kw)
    assert [n for n in await kb(sessionmaker, kw, model=OTHER) if n != "?"] == ["cbt_other"]
    cases: list[dict[str, Any]] = [
        {"approach": "a8-cbt"},
        {"approach": "a8-none-such"},
        {"topics": ["a8-money"]},
        {"topics": ["a8-anger", "a8-trust"]},
        {"level": 2, "topics": ["a8-money", "a8-anger"]},
        {"approach": "a8-cbt", "level": 3},
        {"approach": "a8-gottman", "topics": ["a8-money"]},
        {"level": 4},
    ]
    expected_sets = [
        {"cbt1", "cbt2", "cbt3"},
        set(),
        {"cbt1", "noappr1", "cbt2"},
        {"cbt1", "got1", "got2"},
        {"cbt1", "noappr1", "got1"},
        {"cbt2"},
        set(),
        {"got2"},
    ]
    for case, want in zip(cases, expected_sets, strict=True):
        got = await kb(sessionmaker, kw, **case)
        topics_or_level = "topics" in case or "level" in case or "approach" in case
        assert topics_or_level and set(got) == want and got == await kb_exact(engine, kw, **case), (
            case
        )
    assert [n for n in await kb(sessionmaker, kw, topics=[]) if n != "?"] == mine
    assert len(await kb(sessionmaker, kw, limit=1, approach="a8-cbt")) == 1
    for lim in (0, -1, None):
        assert len(await kb(sessionmaker, kw, limit=lim, approach="a8-cbt")) == 1  # type: ignore[arg-type]
    for lvl in (0, 6, -1):
        with pytest.raises(DBAPIError) as e:
            await kb(sessionmaker, kw, level=lvl)
        assert e.value.orig.sqlstate == "22023"  # type: ignore[union-attr]


async def test_kb_limit_cap_20(engine: AsyncEngine, sessionmaker: SM) -> None:
    """АР-35: предел базы знаний — не больше 20."""
    q = rvec()
    async with engine.begin() as c:
        uid, _ = await person(c)
        d = uuid.uuid4()
        await c.execute(
            text(
                "INSERT INTO kb_documents (id, title, approach, rights_basis, index_status) "
                "VALUES (:i, 't', 'a8-cap', 'a8', 'indexed')"
            ),
            {"i": d},
        )
        await c.execute(
            text(
                "INSERT INTO kb_chunks (id, document_id, text, embedding, embedding_model) "
                "VALUES (gen_random_uuid(), :d, 't', CAST(:v AS vector), :m)"
            ),
            [{"d": d, "v": lit(near(q, 0.5)), "m": MODEL} for _ in range(30)],
        )
    async with user_scope(sessionmaker, uid) as s:
        n = len(
            list(
                await s.execute(
                    text("SELECT id FROM app_kb_nearest(:q, :m, 100, 'a8-cap', NULL, NULL)"),
                    {"q": lit(q), "m": MODEL},
                )
            )
        )
    assert n == 20
    rows = await kb_repo.kb_nearest(sessionmaker, uid, q, MODEL, limit=5, approach="a8-cap")
    assert len(rows) == 5 and all(r.approach == "a8-cap" for r in rows)


async def test_kb_set_chunk_embedding_worker(
    kw: KbWorld, engine: AsyncEngine, sessionmaker: SM
) -> None:
    """Запись вектора фрагмента — воркер; модель пишется; пересчёт переносит фрагмент в поиск
    новой модели; несуществующий — false."""
    cid = kw.chunks["cbt_novec"]
    v = near(kw.q, 0.0)
    assert await _as(engine, "feelings_worker", "SELECT kb_set_chunk_embedding(:c, :v, :m)",
                     c=cid, v=lit(v), m=MODEL) == [(True,)]  # fmt: skip
    assert (await kb(sessionmaker, kw, approach="a8-cbt"))[0] == "cbt_novec"


# =============================================================== 6. Через контекст 2.1


class Const:
    def __init__(self, vec: list[float] | None, model: str = MODEL, fail: bool = False) -> None:
        self._vec, self._model, self._fail = vec, model, fail

    @property
    def model(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return DIM

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self._fail:
            raise EmbeddingError("сеть")
        assert self._vec is not None
        return [list(self._vec) for _ in texts]


@pytest.fixture
async def ctx_world(
    engine: AsyncEngine,
) -> tuple[uuid.UUID, uuid.UUID, list[float], dict[str, uuid.UUID]]:
    q = rvec()
    async with engine.begin() as c:
        uid, agent = await person(c, "Контекст")
        conv = uuid.uuid4()
        await c.execute(text("INSERT INTO conversations (id, agent_id) VALUES (:c, :a)"),
                        {"c": conv, "a": agent})  # fmt: skip
        tid = uuid.uuid4()
        await c.execute(
            text(
                "INSERT INTO conversation_messages (id, conversation_id, author, text_enc) "
                "VALUES (:i, :c, 'owner', :t)"
            ),
            {"i": tid, "c": conv, "t": KEYS.seal(crypto.CONVERSATION_MESSAGE_TEXT, tid, "реплика")},
        )
        base = datetime.now(UTC) - timedelta(days=1)
        ids = {
            "vec_old": await mem(c, agent, near(q, 0.01), body="VECOLD", when=base),
            "vec2": await mem(
                c, agent, near(q, 0.5), body="VEC2", when=base + timedelta(seconds=1)
            ),
            "fresh1": await mem(c, agent, None, body="FRESH1", when=base + timedelta(seconds=10)),
            "fresh2": await mem(c, agent, None, body="FRESH2", when=base + timedelta(seconds=11)),
            "other": await mem(
                c, agent, near(q, 0.0), model=OTHER, body="OTHERM", when=base + timedelta(seconds=5)
            ),
        }
    return uid, conv, q, ids


async def _ctx(sm: SM, w: Any, emb: Any) -> Any:
    uid, conv, _, _ = w
    return await build_context(sm, KEYS, uid, conv, query="реплика", embedder=emb)


async def test_context_vector_then_recency_fill(ctx_world: Any, sessionmaker: SM) -> None:
    """ТЗ §6.1 п.3: по смыслу — только векторы модели эмбеддера (своя память), недобор — по
    свежести без повторов; запись другой модели — только по свежести."""
    _uid, _conv, q, ids = ctx_world
    ctx = await _ctx(sessionmaker, ctx_world, Const(q))
    assert ctx.stats.memory_search == "vector"
    assert list(ctx.stats.memory_ids[:2]) == [ids["vec_old"], ids["vec2"]]
    assert set(ctx.stats.memory_ids) == set(ids.values()) and len(ctx.stats.memory_ids) == 5
    rest = list(ctx.stats.memory_ids[2:])
    assert rest == [ids["fresh2"], ids["fresh1"], ids["other"]]  # свежие первыми


@pytest.mark.parametrize("emb_kind", ["error", "zero", "other_model", "no_records_of_model"])
async def test_context_falls_back_to_recency(
    emb_kind: str, ctx_world: Any, sessionmaker: SM
) -> None:
    """ТЗ §6.1 п.3, АР-35: ошибка эмбеддера, нулевой вектор, модель без записей — память по
    свежести, сборка не падает, транзакция не прерывается (пары и реплики на месте)."""
    _uid, _conv, q, ids = ctx_world
    emb = {
        "error": Const(None, fail=True),
        "zero": Const([0.0] * DIM),
        "other_model": Const(q, model="a8-unknown-model"),
        "no_records_of_model": Const(q, model="a8-none"),
    }[emb_kind]
    ctx = await _ctx(sessionmaker, ctx_world, emb)
    assert ctx.stats.memory_search == "recency"
    assert list(ctx.stats.memory_ids)[:2] == [ids["fresh2"], ids["fresh1"]]
    assert ctx.dialog[-1].text == "реплика"


@pytest.mark.parametrize(
    "emb_kind",
    ["float32_overflow", "long_model_name"],
)
@pytest.mark.xfail(
    strict=True,
    reason="A8-V1 MINOR: вектор, который приложение пропускает, а БД отклоняет 22023 — сборка "
    "контекста падает вместо отката на свежесть",
)
async def test_context_db_rejected_vector_falls_back(
    emb_kind: str, ctx_world: Any, sessionmaker: SM
) -> None:
    """ТЗ §6.1 п.3, АР-35 («без вектора/при ошибке — по свежести»): `check_dim` пропускает
    вектор, который `embedding_arg` отклоняет (компонента 1e39 вне float4; имя модели длиннее
    128) → 22023 внутри общей транзакции
    `read_context` → вся сборка падает (в API — `error internal` вместо ответа). Ожидается:
    такие векторы — откат на свежесть (как нулевой)."""
    _, _, q, ids = ctx_world
    emb = {
        "float32_overflow": Const([1e39, *q[1:]]),
        "long_model_name": Const(q, model="m" * 200),
    }[emb_kind]
    ctx = await _ctx(sessionmaker, ctx_world, emb)
    assert ctx.stats.memory_search == "recency"
    assert ids["fresh2"] in ctx.stats.memory_ids


async def test_context_tiny_norm_vector_still_searches(ctx_world: Any, sessionmaker: SM) -> None:
    """Очень малые (но ненулевые) компоненты — норма не ноль и для БД: поиск по смыслу работает."""
    ctx = await _ctx(sessionmaker, ctx_world, Const([1e-30] * DIM))
    assert ctx.stats.memory_search == "vector"


async def test_hashing_embedder_model_name_fits() -> None:
    """Имя модели заглушки укладывается в 128 символов колонки и функции."""
    assert 0 < len(HashingEmbedder().model) <= 128
    assert math.isfinite(sum((await HashingEmbedder().embed(["деньги"]))[0]))


# =============================================================== 7. /help/contacts публичный


@pytest.fixture
async def http(sessionmaker: SM) -> AsyncIterator[httpx.AsyncClient]:
    def settings() -> Settings:
        return Settings(
            telegram_bot_token=SecretStr("777003:A8-vec-bot-token-not-real"),
            session_secret=SecretStr("a8-vec-session-secret-" + "v" * 24),
            session_ttl_s=900,
            bot_username="A8_Vec_bot",
            env="test",
        )

    saved = app.state.sessionmaker
    app.state.sessionmaker = sessionmaker
    app.dependency_overrides[get_settings] = settings
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://a8") as c:
        yield c
    app.dependency_overrides.clear()
    app.state.sessionmaker = saved


async def test_help_contacts_public(http: httpx.AsyncClient, engine: AsyncEngine) -> None:
    """Б-58, АР-35, ТЗ §9.3 «всегда»: без сессии, с мусорным заголовком, с токеном удалённого
    пользователя — 200, `no-store`, тело одинаково и без данных пользователя; БД не нужна."""
    import time as _time

    from app.core.telegram import sign_init_data

    tg = 750_000_000_000 + _rng.randrange(10**9)
    init = sign_init_data(
        {"auth_date": str(int(_time.time())),
         "user": json.dumps({"id": tg, "first_name": "Удалённая"})},
        "777003:A8-vec-bot-token-not-real",
    )  # fmt: skip
    r = await http.post("/api/v1/auth/telegram", json={"init_data": init})
    assert r.status_code == 200, r.text
    token, uid = r.json()["access_token"], r.json()["me"]["id"]
    async with engine.begin() as c:
        await c.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
    headers_list: list[dict[str, str]] = [
        {},
        {"Authorization": "Bearer garbage.not-a-token"},
        {"Authorization": "Basic abc"},
        {"Authorization": ""},
        {"Authorization": f"Bearer {token}"},
    ]
    bodies = []
    for h in headers_list:
        r = await http.get("/api/v1/help/contacts", headers=h)
        assert r.status_code == 200, (h, r.text)
        assert r.headers["cache-control"] == "no-store"
        assert str(tg) not in r.text and uid not in r.text and "Удалённая" not in r.text
        bodies.append(r.json())
    assert all(b == bodies[0] for b in bodies)
    assert bodies[0] == help_contacts()
    # Без БД: контакты отдаются, даже если сессии БД нет.
    saved, app.state.sessionmaker = app.state.sessionmaker, None
    try:
        r = await http.get("/api/v1/help/contacts")
        assert r.status_code == 200
    finally:
        app.state.sessionmaker = saved


# =============================================================== 8. Обратимость


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — фиксированная команда
        [sys.executable, "-m", "alembic", "-x", f"db_url={url}", *args],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        check=False,
    )


async def test_migration_reversible_and_check(engine: AsyncEngine) -> None:
    """Кр.4: в отдельной БД — upgrade head → downgrade 0007 (функции, индексы, CHECK, колонки
    модели удалены, векторы остались) → upgrade head → `alembic check` чист."""
    name = f"feelings_a8v_{os.getpid()}"
    admin = TEST_DATABASE_URL.rsplit("/", 1)[0] + "/postgres"
    url = TEST_DATABASE_URL.rsplit("/", 1)[0] + f"/{name}"
    from sqlalchemy.ext.asyncio import create_async_engine

    adm = create_async_engine(admin, isolation_level="AUTOCOMMIT")
    async with adm.connect() as c:
        await c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        await c.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        assert _alembic(url, "upgrade", "head").returncode == 0
        eng = create_async_engine(url)
        async with eng.begin() as c:
            aid = uuid.uuid4()
            uid = uuid.uuid4()
            await c.execute(text("INSERT INTO users (id, telegram_id) VALUES (:i, 1)"), {"i": uid})
            await c.execute(
                text("INSERT INTO agents (id, user_id) VALUES (:a, :u)"), {"a": aid, "u": uid}
            )
            await c.execute(
                text(
                    "INSERT INTO memory_items (agent_id, type, text_enc, source, embedding, "
                    "embedding_model) VALUES (:a, 'fact', 'x', 'owner_dialog', CAST(:v AS vector), 'm')"
                ),
                {"a": aid, "v": lit(rvec())},
            )
        await eng.dispose()
        r = _alembic(url, "downgrade", "0007")
        assert r.returncode == 0, r.stderr[-2000:]
        eng = create_async_engine(url)
        async with eng.connect() as c:
            left = (
                await c.execute(
                    text(
                        "SELECT count(*) FROM pg_proc WHERE proname IN ('app_memory_nearest', "
                        "'app_set_memory_embedding', 'app_kb_nearest', 'kb_set_chunk_embedding', "
                        "'embedding_arg')"
                    )
                )
            ).scalar_one()
            cols = (
                await c.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE column_name = 'embedding_model'"
                    )
                )
            ).scalar_one()
            idx = (
                await c.execute(
                    text("SELECT count(*) FROM pg_indexes WHERE indexname LIKE '%embedding_hnsw'")
                )
            ).scalar_one()
            vecs = (
                await c.execute(
                    text("SELECT count(*) FROM memory_items WHERE embedding IS NOT NULL")
                )
            ).scalar_one()
        await eng.dispose()
        assert (left, cols, idx, vecs) == (0, 0, 0, 1)
        assert _alembic(url, "upgrade", "head").returncode == 0
        chk = _alembic(url, "check")
        assert chk.returncode == 0, chk.stdout[-2000:] + chk.stderr[-2000:]
    finally:
        async with adm.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await adm.dispose()

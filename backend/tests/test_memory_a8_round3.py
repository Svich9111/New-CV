"""2.4, A8, раунд 3: независимая проверка исправлений A3 (`e3afe4b`) и A1 (`e7eb8bd`, `d5c32f7`; АР-38):
гонки сжатия чата с удалением записи памяти, пометка «на очистку», взаимоблокировки с удалением
аккаунта, итог очистки. Помощники — свои (`tests/test_memory_a8*.py`, `tests/test_chat_a8.py`).
Сеть не используется. Дефекты — `xfail(strict=True)` (docs/reviews/2.4-test.md, «Раунд 3»)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.ai import memory as mem_ai
from app.ai.llm import StructuredResult, Usage
from app.ai.summary import CompressionPolicy, compress_history
from app.api.chat import ChatRuntime
from app.db import user_scope
from app.main import app
from app.repositories import chat as chat_repo
from app.repositories import memory as memory_repo
from app.repositories import memory_work as mw
from tests.test_chat_a8 import KEYS, FakeModel
from tests.test_chat_a8 import models_of as chat_models
from tests.test_memory_a8 import (
    FP,
    MemModel,
    P,
    add_mem,
    ago,
    claim_for,
    models_of,
    person,
    release_others,
    say,
)
from tests.test_memory_a8_round2 import _login, agent_sees, put_summary, raw_summary

SM = async_sessionmaker[AsyncSession]
SECRET = "СЕКРЕТa8 работает ночами"
POLICY = CompressionPolicy(trigger_turns=7, keep_turns=6)


@pytest.fixture
async def http(sessionmaker: SM) -> AsyncIterator[httpx.AsyncClient]:
    from pydantic import SecretStr

    from app.config import Settings, get_settings

    def settings() -> Settings:
        return Settings(
            telegram_bot_token=SecretStr("777005:A8-mem2-bot-token-not-real"),
            session_secret=SecretStr("a8-mem2-session-secret-" + "q" * 24),
            session_ttl_s=900,
            bot_username="A8_Mem2_bot",
            env="test",
        )

    saved = (app.state.sessionmaker, app.state.keyring, app.state.chat)
    app.state.sessionmaker, app.state.keyring = sessionmaker, KEYS
    app.state.chat = ChatRuntime()
    app.dependency_overrides[get_settings] = settings
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://a8") as c:
        yield c
    app.dependency_overrides.clear()
    app.state.sessionmaker, app.state.keyring, app.state.chat = saved


async def chat_world(engine: AsyncEngine, name: str) -> tuple[P, uuid.UUID]:
    async with engine.begin() as c:
        p = await person(c, name)
        item = await add_mem(c, p, SECRET)
        t0 = await say(c, p, "owner", "старт", ago(0))
        for i in range(1, 12):
            await say(c, p, "owner" if i % 2 else "agent", f"реплика {i}", ago(i))
    await put_summary(engine, p, f"{SECRET}. Любит чай.", t0)
    return p, item


# =============================================================== 1. Гонка сжатия чата


@pytest.mark.parametrize("point", ["before_read", "during_model", "before_write"])
async def test_chat_compression_race_every_point(
    point: str, engine: AsyncEngine, sessionmaker: SM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ревью р2 MAJOR-2 / A8-M2, ТЗ §2 п.6, АР-37: удаление записи памяти в каждой точке сжатия
    истории чата — до чтения сводки, во время вызова модели, между ответом модели и записью.
    Удалённое не возвращается во вход агента; итоговая сводка либо помечена «на очистку»
    (скрыта), либо построена без прежнего текста."""
    p, item = await chat_world(engine, f"Точка-{point}")
    gate = asyncio.Event()
    summ = FakeModel(summary={"summary": f"{SECRET}. Новое сжатие."}, summary_gate=gate)
    if point == "before_read":
        assert await mem_ai.forget_memory(sessionmaker, KEYS, p.uid, item) is not None
        gate.set()
    if point == "before_write":
        real = chat_repo.save_history_summary
        fired = False

        async def forget_then_save(*a: Any, **k: Any) -> bool:
            nonlocal fired
            if not fired:
                fired = True
                assert await mem_ai.forget_memory(sessionmaker, KEYS, p.uid, item) is not None
            return await real(*a, **k)

        monkeypatch.setattr(chat_repo, "save_history_summary", forget_then_save)
        gate.set()
    task = asyncio.create_task(
        compress_history(
            sessionmaker, KEYS, p.uid, p.conv, chat_models(summ, summ=summ), policy=POLICY
        )
    )
    if point == "during_model":
        for _ in range(200):
            if summ.kinds("summary"):
                break
            await asyncio.sleep(0.01)
        assert await mem_ai.forget_memory(sessionmaker, KEYS, p.uid, item) is not None
        gate.set()
    res = await task
    assert res.status in ("saved", "conflict")
    assert "СЕКРЕТa8" not in await agent_sees(sessionmaker, p), point
    raw = await raw_summary(engine, p)
    assert raw is not None
    if SECRET in raw["text"]:
        assert raw.get("forget") == [str(item)]  # текст с удалённым — только под пометкой
    # Вход модели после удаления не содержит прежней сводки.
    calls = summ.kinds("summary")
    if point != "during_model":
        last = json.loads(calls[-1]["messages"][0][1])
        assert "СЕКРЕТa8" not in last["previous_summary"]


async def test_mark_removed_by_forget_after_chat_compression_carried_it(
    engine: AsyncEngine, sessionmaker: SM
) -> None:
    """АР-37: сжатие чата перенесло пометку в новую сводку — фоновая очистка (`run_forget`) её
    снимает: сводка снова видна, без удалённого (дословный остаток → пусто)."""
    p, item = await chat_world(engine, "Перенос")
    job = await mem_ai.forget_memory(sessionmaker, KEYS, p.uid, item)
    assert job is not None
    summ = FakeModel(summary={"summary": f"{SECRET}. Из реплик."})
    res = await compress_history(
        sessionmaker, KEYS, p.uid, p.conv, chat_models(summ, summ=summ), policy=POLICY
    )
    assert res.status == "saved"
    raw = await raw_summary(engine, p)
    assert raw is not None and raw["forget"] == [str(item)]

    class Echo(MemModel):
        async def complete_json(self, **kw: Any) -> Any:
            if "rewritten" in kw["schema"]["properties"]:
                pl = json.loads(kw["messages"][-1].content)
                # модель «забыла» плохо: удалённое осталось дословно, другим регистром
                body = pl["summary"].replace(SECRET, SECRET.upper() + "!")
                return StructuredResult(data={"rewritten": body}, model="x", usage=Usage(),
                                        latency_s=0, cost_usd=0)  # fmt: skip
            return await super().complete_json(**kw)

    out = await mem_ai.run_forget(sessionmaker, KEYS, models_of(Echo(profile="П")), job)
    assert out.status == "done"
    raw = await raw_summary(engine, p)
    assert raw is not None and "forget" not in raw and raw["text"] == ""


@pytest.mark.xfail(
    strict=True,
    reason="A8-M3 MINOR: чат переписал помеченную сводку во время сжатия памяти — сжатие её "
    "пропустило и сняло запрос; сводка скрыта до следующего периода",
)
async def test_marked_summary_not_hidden_forever_under_activity(
    engine: AsyncEngine, sessionmaker: SM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """АР-37 п.2 «не выполнилась — сжатие обнуляет помеченную»: очистки нет (моделей не было),
    сжатие памяти идёт, а чат в это время переписывает помеченную сводку (перенося пометку).
    Сжатие памяти пропускает изменившуюся сводку (`replace_summary_in` со сверкой) и снимает
    запрос сжатия; при периоде 7 дней сводка остаётся скрытой (непрерывность диалога потеряна)
    до следующего периода. Ожидается: помеченная сводка, не очищенная сжатием, оставляет запрос
    сжатия (или сжатие повторяет попытку)."""
    p, item = await chat_world(engine, "Активный")
    async with engine.begin() as c:
        await add_mem(c, p, "живая запись")  # чтобы модель сводки профиля вызывалась
    assert await mem_ai.forget_memory(sessionmaker, KEYS, p.uid, item) is not None
    every = timedelta(days=7)

    async def claim() -> list[Any]:
        return [x for x in await mw.claim_compaction(async_sessionmaker(engine), every=every,
                lease=timedelta(minutes=5), limit=100) if x.agent_id == p.agent]  # fmt: skip

    [comp] = await claim()

    async def chat_rewrites() -> None:
        summ = FakeModel(summary={"summary": "Новое сжатие."})
        res = await compress_history(sessionmaker, KEYS, p.uid, p.conv,
                                     chat_models(summ, summ=summ), policy=POLICY)  # fmt: skip
        assert res.status == "saved"

    m = MemModel(profile="П", hooks={"profile": chat_rewrites})
    out = await mem_ai.run_compaction(sessionmaker, KEYS, FP, models_of(m), comp)
    assert out.status == "done"
    raw = await raw_summary(engine, p)
    assert raw is not None
    still_hidden = bool(raw.get("forget"))
    assert not still_hidden or len(await claim()) == 1
    assert "СЕКРЕТa8" not in await agent_sees(sessionmaker, p)


# =============================================================== 2. Взаимоблокировки


async def _waiting_on_lock(engine: AsyncEngine, n: int = 1, wait_s: float = 5.0) -> bool:
    for _ in range(int(wait_s / 0.05)):
        async with engine.connect() as c:
            k = (
                await c.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                        "AND datname = current_database()"
                    )
                )
            ).scalar_one()
        if k >= n:
            return True
        await asyncio.sleep(0.05)
    return False


def _no_deadlock(results: list[Any]) -> None:
    for r in results:
        if isinstance(r, DBAPIError):
            assert getattr(r.orig, "sqlstate", None) != "40P01", r
            raise r
        if isinstance(r, BaseException):
            raise r


async def _delete_account(sm: SM, uid: uuid.UUID) -> Any:
    async with user_scope(sm, uid) as s:
        return (await s.execute(text("SELECT app_delete_account()"))).scalar()


async def _op_world(engine: AsyncEngine, name: str) -> tuple[P, uuid.UUID]:
    async with engine.begin() as c:
        p = await person(c, name)
        item = await add_mem(c, p, "любит горы")
        t0 = await say(c, p, "owner", "старт", ago(0))
        for i in range(1, 12):
            await say(c, p, "owner" if i % 2 else "agent", f"р{i}", ago(i))
    await put_summary(engine, p, "любит горы. чай.", t0)
    await release_others(engine, p)
    return p, item


async def _op(kind: str, engine: AsyncEngine, sm: SM, p: P, item: uuid.UUID) -> Any:
    """Одна операция, которая пишет память/сводки владельца."""
    if kind == "forget":
        return await mem_ai.forget_memory(sm, KEYS, p.uid, item)
    if kind == "delete_fn":
        try:
            return await memory_repo.delete_memory(sm, p.uid, item)
        except memory_repo.MemoryOwnerGone:
            return "gone"
    if kind == "vector":
        try:
            return await chat_repo.set_memory_embedding(sm, p.uid, item, [1.0] * 1024, "m")
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == "42501":
                return "gone"
            raise
    if kind == "extract":
        claim = await claim_for(engine, p)
        if claim is None:
            return "no-claim"
        return (await mem_ai.run_extraction(sm, KEYS, FP, models_of(MemModel(items=["x"])),
                                            claim)).status  # fmt: skip
    if kind == "compact":
        comp = [x for x in await mw.claim_compaction(async_sessionmaker(engine),
                every=timedelta(minutes=1), lease=timedelta(minutes=5), limit=100)
                if x.agent_id == p.agent]  # fmt: skip
        if not comp:
            return "no-claim"
        return (await mem_ai.run_compaction(sm, KEYS, FP, models_of(MemModel()), comp[0])).status
    if kind == "chat_compress":
        summ = FakeModel(summary={"summary": "s"})
        return (await compress_history(sm, KEYS, p.uid, p.conv, chat_models(summ, summ=summ),
                                       policy=POLICY)).status  # fmt: skip
    if kind == "forget_job":
        job = await mem_ai.forget_memory(sm, KEYS, p.uid, item)
        if job is None:
            return "gone"
        return (await mem_ai.run_forget(sm, KEYS, models_of(MemModel(profile="П")), job)).status
    raise AssertionError(kind)


OPS = ["forget", "delete_fn", "vector", "extract", "compact", "chat_compress", "forget_job"]
# Точки, где операция держит часть блокировок и ждёт (имитация медленной транзакции).
PAUSE: dict[str, tuple[Any, str]] = {
    "forget": (mw, "delete_memory_in"),
    "forget_job": (mw, "replace_summary_in"),
    "extract": (mw, "extraction_done_in"),
    "compact": (mw, "compaction_done_in"),
}


@pytest.mark.parametrize("order", ["account_first", "op_first"])
@pytest.mark.parametrize("op", OPS)
async def test_no_deadlock_with_account_deletion(
    op: str, order: str, engine: AsyncEngine, sessionmaker: SM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ревью р2 MAJOR-1, АР-13/АР-38: каждая запись памяти и сводок против удаления аккаунта,
    оба порядка. `account_first`: транзакция удаления аккаунта открыта (блокировки взяты), операция
    стартует и ждёт, удаление фиксируется. `op_first`: операция взяла первые блокировки и стоит
    посреди транзакции, удаление аккаунта стартует и ждёт, операция продолжает. Ни 40P01, ни
    необработанных ошибок; аккаунт удалён без следов."""
    p, item = await _op_world(engine, f"DL-{op}-{order}")
    if order == "account_first":
        conn = await engine.connect()
        tx = await conn.begin()
        await conn.execute(text("SET LOCAL ROLE feelings_app"))
        await conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(p.uid)})
        await conn.execute(text("SELECT app_delete_account()"))
        task = asyncio.create_task(_op(op, engine, sessionmaker, p, item))
        await asyncio.sleep(0.4)
        await tx.commit()
        await conn.close()
        results: list[Any] = list(await asyncio.gather(task, return_exceptions=True))
    else:
        release = asyncio.Event()
        paused = asyncio.Event()
        if op in PAUSE:
            module, name = PAUSE[op]
            real = getattr(module, name)

            async def paused_call(*a: Any, **k: Any) -> Any:
                if not paused.is_set():
                    paused.set()
                    await release.wait()
                return await real(*a, **k)

            monkeypatch.setattr(module, name, paused_call)
            task = asyncio.create_task(_op(op, engine, sessionmaker, p, item))
            await asyncio.wait_for(paused.wait(), 10)
        else:
            task = asyncio.create_task(_op(op, engine, sessionmaker, p, item))
            await asyncio.sleep(0)
        deleter = asyncio.create_task(_delete_account(sessionmaker, p.uid))
        waited = await _waiting_on_lock(engine, 1, wait_s=1.0)
        if op in PAUSE:
            assert waited, "удаление аккаунта не ждало операцию — порядок не проверен"
        release.set()
        results = list(await asyncio.gather(task, deleter, return_exceptions=True))
    _no_deadlock(list(results))
    async with engine.connect() as c:
        left = (await c.execute(text("SELECT count(*) FROM users WHERE id = :u"),
                                {"u": p.uid})).scalar_one()  # fmt: skip
        mem_left = (await c.execute(text("SELECT count(*) FROM memory_items WHERE agent_id = :a"),
                                    {"a": p.agent})).scalar_one()  # fmt: skip
    assert (left, mem_left) == (0, 0)


async def test_api_delete_memory_during_account_deletion(
    http: httpx.AsyncClient, engine: AsyncEngine, sessionmaker: SM
) -> None:
    """`DELETE /memory/{id}`, пока удаляется аккаунт (транзакция удаления открыта): запрос ждёт и
    получает 404 или 401 — не 500 и не 409; аккаунта нет."""
    uid, h = await _login(http)
    async with engine.begin() as c:
        await c.execute(text("INSERT INTO agents (id, user_id) VALUES (gen_random_uuid(), :u)"),
                        {"u": uid})  # fmt: skip
        agent = (await c.execute(text("SELECT id FROM agents WHERE user_id = :u"),
                                 {"u": uid})).scalar_one()  # fmt: skip
        p = P(uid, agent, uuid.uuid4())
        item = await add_mem(c, p, "x")
    for order in ("account_first", "concurrent"):
        if order == "account_first":
            conn = await engine.connect()
            tx = await conn.begin()
            await conn.execute(text("SET LOCAL ROLE feelings_app"))
            await conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(uid)})
            await conn.execute(text("SELECT app_delete_account()"))
            req = asyncio.create_task(http.delete(f"/api/v1/memory/{item}", headers=h))
            await asyncio.sleep(0.4)
            await tx.commit()
            await conn.close()
            r = await req
            assert r.status_code in (401, 404), r.text
        else:
            uid2, h2 = await _login(http)
            async with engine.begin() as c:
                await c.execute(text("INSERT INTO agents (id, user_id) VALUES "
                                     "(gen_random_uuid(), :u)"), {"u": uid2})  # fmt: skip
                ag2 = (await c.execute(text("SELECT id FROM agents WHERE user_id = :u"),
                                       {"u": uid2})).scalar_one()  # fmt: skip
                items = [await add_mem(c, P(uid2, ag2, uuid.uuid4()), f"x{i}") for i in range(5)]
            reqs = [http.delete(f"/api/v1/memory/{i}", headers=h2) for i in items]
            out = await asyncio.gather(*reqs, _delete_account(sessionmaker, uid2),
                                       return_exceptions=True)  # fmt: skip
            _no_deadlock([x for x in out if isinstance(x, BaseException)])
            codes = [x.status_code for x in out[:5] if isinstance(x, httpx.Response)]
            assert codes and all(c in (204, 401, 404) for c in codes), codes


# =============================================================== 3. Итог очистки


@pytest.mark.parametrize(
    ("rewritten", "kept"),
    [
        (f"Любит чай. {SECRET}", False),
        (f"Любит чай. {SECRET.upper()}!!!", False),
        (f"Любит чай, а ещё он {SECRET.lower()}, говорят.", False),
        ("Любит чай.", True),
        ("Любит чай. Трудится по ночам.", True),  # перефразировка — не ловится (АР-36)
    ],
    ids=["verbatim", "case_punct", "inside_sentence", "clean", "paraphrase"],
)
async def test_forget_result_check(
    rewritten: str, kept: bool, engine: AsyncEngine, sessionmaker: SM
) -> None:
    """Ревью р2 MINOR-1: модель очистки вернула сводку, где удалённое осталось дословно (в т.ч.
    другим регистром, с пунктуацией, внутри предложения) — сводка пустая; чистая — сохраняется;
    перефразировка — известное ограничение (сохраняется)."""
    p, item = await chat_world(engine, f"Итог-{len(rewritten)}")
    job = await mem_ai.forget_memory(sessionmaker, KEYS, p.uid, item)
    assert job is not None

    class Fixed(MemModel):
        async def complete_json(self, **kw: Any) -> Any:
            if "rewritten" in kw["schema"]["properties"]:
                return StructuredResult(data={"rewritten": rewritten}, model="x", usage=Usage(),
                                        latency_s=0, cost_usd=0)  # fmt: skip
            return await super().complete_json(**kw)

    assert (await mem_ai.run_forget(sessionmaker, KEYS, models_of(Fixed()), job)).status == "done"
    raw = await raw_summary(engine, p)
    assert raw is not None and "forget" not in raw
    assert (raw["text"] == rewritten) == kept and (raw["text"] == "") == (not kept)

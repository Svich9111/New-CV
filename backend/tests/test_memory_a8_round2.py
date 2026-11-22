"""2.4, A8, раунд 2: независимая проверка исправлений (A3 `9ae1b31`, A2 `f671eed`, A1 0010 `5604370`;
АР-37, АР-38). Помощники — свои (`tests/test_memory_a8.py`, `tests/test_chat_a8.py`). Сеть не
используется. Дефекты — `xfail(strict=True)` (docs/reviews/2.4-test.md, «Раунд 2»)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.ai import memory as mem_ai
from app.ai.context import build_context
from app.ai.llm import LLMUnavailable
from app.ai.summary import CompressionPolicy, compress_history
from app.api.chat import ChatRuntime
from app.config import Settings, get_settings
from app.core import crypto
from app.db import user_scope
from app.main import app
from app.repositories import memory_work as mw
from tests.test_chat_a8 import KEYS, FakeModel, _pmsg, summary_envelope
from tests.test_chat_a8 import models_of as chat_models
from tests.test_memory_a8 import (
    FP,
    FPK,
    GAP,
    MemModel,
    P,
    add_mem,
    ago,
    claim_for,
    memory_of,
    models_of,
    my_fp,
    person,
    release_others,
    say,
)

SM = async_sessionmaker[AsyncSession]
SECRET = "СЕКРЕТa8 работает ночами"


async def put_summary(engine: AsyncEngine, p: P, body: str, through: uuid.UUID) -> None:
    async with engine.begin() as c:
        await c.execute(
            text("UPDATE conversations SET summary_enc = :s WHERE id = :c"),
            {
                "s": KEYS.seal(
                    crypto.CONVERSATION_SUMMARY, p.conv, summary_envelope(body, ago(1), through)
                ),
                "c": p.conv,
            },
        )


async def put_profile(engine: AsyncEngine, p: P, body: str) -> None:
    async with engine.begin() as c:
        await c.execute(
            text("UPDATE agents SET profile_summary_enc = :s WHERE id = :a"),
            {"s": KEYS.seal(crypto.AGENT_PROFILE_SUMMARY, p.agent, body), "a": p.agent},
        )


async def raw_summary(engine: AsyncEngine, p: P) -> dict[str, Any] | None:
    async with engine.connect() as c:
        blob = (await c.execute(text("SELECT summary_enc FROM conversations WHERE id = :c"),
                                {"c": p.conv})).scalar_one()  # fmt: skip
    return (
        None if blob is None else json.loads(KEYS.open(crypto.CONVERSATION_SUMMARY, p.conv, blob))
    )


async def agent_sees(sm: SM, p: P, query: str = "новое") -> str:
    """Всё, что ушло бы во вход агента 2.1 (профиль, сводка, реплики, память)."""
    ctx = await build_context(sm, KEYS, p.uid, p.conv, query=query)
    return json.dumps(ctx.payload(), ensure_ascii=False)


@pytest.fixture
async def secret_world(engine: AsyncEngine) -> tuple[P, uuid.UUID, uuid.UUID]:
    """Запись памяти с секретом; тот же секрет — в сводке диалога и в сводке профиля."""
    async with engine.begin() as c:
        p = await person(c, "Секрет")
        item = await add_mem(c, p, SECRET)
        await add_mem(c, p, "двое детей")
        t1 = await say(c, p, "owner", "старое", ago(1))
        await say(c, p, "owner", "новое", ago(2))
    await put_summary(engine, p, f"{SECRET}. Любит чай.", t1)
    await put_profile(engine, p, f"ПРОФИЛЬ: {SECRET}")
    return p, item, t1


# =============================================================== 1. Удалённое не в контексте


async def test_immediately_after_delete_nothing_in_context(
    secret_world: tuple[P, uuid.UUID, uuid.UUID], engine: AsyncEngine, sessionmaker: SM
) -> None:
    """АР-37 п.1–2: сразу после удаления (до любой фоновой задачи) — профиль обнулён, сводка
    помечена и во вход агента не идёт; выгрузка ФТ-21 — тоже без неё; отметка сводки цела."""
    p, item, t1 = secret_world
    assert SECRET in await agent_sees(sessionmaker, p)
    job = await mem_ai.forget_memory(sessionmaker, KEYS, p.uid, item)
    assert job is not None and SECRET not in repr(job)
    assert "СЕКРЕТa8" not in await agent_sees(sessionmaker, p)
    raw = await raw_summary(engine, p)
    assert raw is not None and raw["forget"] == [str(item)] and raw["through_id"] == str(t1)
    from app.repositories.chat import summary_text

    assert summary_text(json.dumps(raw, ensure_ascii=False)) == ""
    async with engine.connect() as c:
        prof = (await c.execute(text("SELECT profile_summary_enc FROM agents WHERE id = :a"),
                                {"a": p.agent})).scalar_one()  # fmt: skip
    assert prof is None


@pytest.mark.parametrize("mode", ["ok", "model_fails"])
async def test_run_forget_rewrites_or_empties(
    mode: str, secret_world: tuple[P, uuid.UUID, uuid.UUID], engine: AsyncEngine, sessionmaker: SM
) -> None:
    """АР-37 п.2: фоновая очистка — модель получает сводку и удалённый текст (только в памяти
    процесса), переписанная сводка без пометки возвращается в контекст; сбой модели — сводка
    обнулена с сохранением отметки; профиль пересобран из живых записей."""
    p, item, t1 = secret_world
    job = await mem_ai.forget_memory(sessionmaker, KEYS, p.uid, item)
    assert job is not None
    calls: list[dict[str, Any]] = []

    class Rewriter(MemModel):
        async def complete_json(self, **kw: Any) -> Any:
            if "rewritten" in kw["schema"]["properties"]:
                payload = json.loads(kw["messages"][-1].content)
                calls.append(payload)
                if mode == "model_fails":
                    raise LLMUnavailable("down")
                from app.ai.llm import StructuredResult, Usage

                return StructuredResult(data={"rewritten": "Любит чай."}, model="x",
                                        usage=Usage(), latency_s=0, cost_usd=0)  # fmt: skip
            return await super().complete_json(**kw)

    rw = Rewriter(profile="Двое детей.")
    out = await mem_ai.run_forget(sessionmaker, KEYS, models_of(rw), job)
    assert out.status == "done"
    assert calls and calls[0]["forget"] == SECRET and SECRET in calls[0]["summary"]
    raw = await raw_summary(engine, p)
    assert raw is not None and "forget" not in raw and raw["through_id"] == str(t1)
    assert raw["text"] == ("Любит чай." if mode == "ok" else "")
    seen = await agent_sees(sessionmaker, p)
    assert "СЕКРЕТa8" not in seen
    assert ("Любит чай." in seen) == (mode == "ok")
    assert "Двое детей." in seen
    [prof_call] = rw.of("profile")
    assert "СЕКРЕТa8" not in json.dumps(prof_call["payload"], ensure_ascii=False)


async def test_two_deletions_one_summary(engine: AsyncEngine, sessionmaker: SM) -> None:
    """Сводка с двумя удалёнными записями: пометки копятся; очистка первой оставляет сводку
    скрытой (вторая ещё в ней); после второй — сводка видна и без обеих; при параллельных
    очистках обе записи учтены (сверка и повтор)."""
    async with engine.begin() as c:
        p = await person(c, "Два")
        a = await add_mem(c, p, "ПЕРВОЕa8")
        b = await add_mem(c, p, "ВТОРОЕa8")
        t1 = await say(c, p, "owner", "старое", ago(1))
        await say(c, p, "owner", "новое", ago(2))
    await put_summary(engine, p, "ПЕРВОЕa8. ВТОРОЕa8. ЧАЙa8.", t1)
    ja = await mem_ai.forget_memory(sessionmaker, KEYS, p.uid, a)
    jb = await mem_ai.forget_memory(sessionmaker, KEYS, p.uid, b)
    assert ja and jb
    raw = await raw_summary(engine, p)
    assert raw is not None and raw["forget"] == [str(a), str(b)]

    class Strip(MemModel):
        async def complete_json(self, **kw: Any) -> Any:
            if "rewritten" in kw["schema"]["properties"]:
                pl = json.loads(kw["messages"][-1].content)
                await asyncio.sleep(0.05)
                from app.ai.llm import StructuredResult, Usage

                body = ". ".join(x for x in pl["summary"].split(". ") if pl["forget"] not in x)
                return StructuredResult(data={"rewritten": body}, model="x", usage=Usage(),
                                        latency_s=0, cost_usd=0)  # fmt: skip
            return await super().complete_json(**kw)

    out = await asyncio.gather(
        mem_ai.run_forget(sessionmaker, KEYS, models_of(Strip()), ja),
        mem_ai.run_forget(sessionmaker, KEYS, models_of(Strip()), jb),
    )
    assert all(o.status == "done" for o in out)
    raw = await raw_summary(engine, p)
    assert raw is not None and "forget" not in raw
    assert "ПЕРВОЕa8" not in raw["text"] and "ВТОРОЕa8" not in raw["text"]
    seen = await agent_sees(sessionmaker, p)
    assert "ПЕРВОЕa8" not in seen and "ВТОРОЕa8" not in seen


async def test_after_restart_compaction_empties_marked(
    secret_world: tuple[P, uuid.UUID, uuid.UUID], engine: AsyncEngine, sessionmaker: SM
) -> None:
    """АР-37 п.2, АР-38: задача очистки не выполнилась (перезапуск) — сжатие, поставленное
    удалением вне периода, обнуляет помеченную сводку (отметка цела) и пересобирает профиль."""
    p, item, t1 = secret_world
    assert await mem_ai.forget_memory(sessionmaker, KEYS, p.uid, item) is not None
    comp = [x for x in await mw.claim_compaction(async_sessionmaker(engine),
            every=timedelta(days=7), lease=timedelta(minutes=5), limit=100)
            if x.agent_id == p.agent]  # fmt: skip
    assert len(comp) == 1
    out = await mem_ai.run_compaction(sessionmaker, KEYS, FP, models_of(MemModel(profile="П")),
                                      comp[0])  # fmt: skip
    assert out.status == "done"
    raw = await raw_summary(engine, p)
    assert raw is not None
    assert raw == {"v": 1, "text": "", "through_at": raw["through_at"], "through_id": str(t1)}
    assert "СЕКРЕТa8" not in await agent_sees(sessionmaker, p)


async def test_delete_during_compaction_requests_another(
    engine: AsyncEngine, sessionmaker: SM
) -> None:
    """АР-38: удаление во время сжатия — отметка новее захвата не снимается: агент выдаётся ещё
    раз сразу (вне периода); без новых удалений — нет."""
    async with engine.begin() as c:
        p = await person(c, "ВоВремя")
        a = await add_mem(c, p, "раз")
        b = await add_mem(c, p, "два")
    every = timedelta(days=7)

    async def claim() -> list[Any]:
        return [x for x in await mw.claim_compaction(async_sessionmaker(engine), every=every,
                lease=timedelta(minutes=5), limit=100) if x.agent_id == p.agent]  # fmt: skip

    assert await mem_ai.forget_memory(sessionmaker, KEYS, p.uid, a) is not None
    [first] = await claim()

    async def delete_b() -> None:
        assert await mem_ai.forget_memory(sessionmaker, KEYS, p.uid, b) is not None

    m = MemModel(profile="П", hooks={"profile": delete_b})
    out = await mem_ai.run_compaction(sessionmaker, KEYS, FP, models_of(m), first)
    assert out.status in ("lost", "done")
    if out.status == "lost":
        async with engine.begin() as c:  # аренда истекла бы — здесь снимаем сразу
            await c.execute(text("UPDATE agents SET memory_lease_until = now() - interval '1 s' "
                                 "WHERE id = :a"), {"a": p.agent})  # fmt: skip
    again = await claim()
    assert len(again) == 1
    out2 = await mem_ai.run_compaction(sessionmaker, KEYS, FP, models_of(MemModel()), again[0])
    assert out2.status == "done"
    assert await claim() == []


async def test_delete_of_new_record_during_successful_compaction_requests_another(
    engine: AsyncEngine, sessionmaker: SM
) -> None:
    """АР-38: запись появилась и была удалена владельцем во время сжатия, которое завершилось
    успешно (её не было среди прочитанных — отката нет). Отметка удаления новее захвата — агент
    выдаётся на сжатие ещё раз сразу (вне периода)."""
    async with engine.begin() as c:
        p = await person(c, "Новая")
        await add_mem(c, p, "старое")
    every = timedelta(days=7)

    async def claim() -> list[Any]:
        return [x for x in await mw.claim_compaction(async_sessionmaker(engine), every=every,
                lease=timedelta(minutes=5), limit=100) if x.agent_id == p.agent]  # fmt: skip

    [first] = await claim()

    async def add_and_forget() -> None:
        async with engine.begin() as c:
            fresh = await add_mem(c, p, "НОВОЕa8")
        assert await mem_ai.forget_memory(sessionmaker, KEYS, p.uid, fresh) is not None

    m = MemModel(profile="П", hooks={"profile": add_and_forget})
    out = await mem_ai.run_compaction(sessionmaker, KEYS, FP, models_of(m), first)
    assert out.status == "done"
    assert len(await claim()) == 1


async def test_shutdown_leaves_summaries_hidden(
    secret_world: tuple[P, uuid.UUID, uuid.UUID], engine: AsyncEngine, sessionmaker: SM
) -> None:
    """Остановка сервиса снимает фоновую очистку — сводки остаются скрытыми (не возвращаются в
    контекст), их обнулит сжатие."""
    p, item, _ = secret_world
    job = await mem_ai.forget_memory(sessionmaker, KEYS, p.uid, item)
    assert job is not None
    gate = asyncio.Event()

    class Slow(MemModel):
        async def complete_json(self, **kw: Any) -> Any:
            await gate.wait()
            return await super().complete_json(**kw)

    rt = ChatRuntime()
    rt.spawn(mem_ai.run_forget(sessionmaker, KEYS, models_of(Slow()), job))  # type: ignore[arg-type]
    await asyncio.sleep(0.1)
    await rt.aclose()
    raw = await raw_summary(engine, p)
    assert raw is not None and raw.get("forget") == [str(item)]
    assert "СЕКРЕТa8" not in await agent_sees(sessionmaker, p)


@pytest.mark.parametrize(
    "order",
    [
        pytest.param(
            "chat_reads_before_forget",
            marks=pytest.mark.xfail(
                strict=True,
                reason="A8-M2 MAJOR: сжатие чата, прочитавшее сводку до удаления, записывает её "
                "после — без пометки «на очистку», удалённое возвращается в контекст",
            ),
        ),
        "chat_reads_after_forget",
    ],
)
async def test_concurrent_chat_compression_during_forget(
    order: str, engine: AsyncEngine, sessionmaker: SM
) -> None:
    """АР-37, ТЗ §2 п.6: сжатие истории чата (2.1) идёт одновременно с удалением записи памяти.
    (а) чат прочитал сводку до удаления, записал после; (б) чат начал после удаления. Удалённое
    не должно вернуться во вход агента ни через сводку, ни через вход модели сжатия."""
    async with engine.begin() as c:
        p = await person(c, "ГонкаЧата")
        item = await add_mem(c, p, SECRET)
        t0 = await say(c, p, "owner", "старт", ago(0))
        for i in range(1, 12):
            await say(c, p, "owner" if i % 2 else "agent", f"реплика {i}", ago(i))
    await put_summary(engine, p, f"{SECRET}. Любит чай.", t0)
    gate = asyncio.Event()
    summ = FakeModel(summary={"summary": f"{SECRET}. Новое сжатие."}, summary_gate=gate)
    policy = CompressionPolicy(trigger_turns=7, keep_turns=6)
    if order == "chat_reads_after_forget":
        assert await mem_ai.forget_memory(sessionmaker, KEYS, p.uid, item) is not None
        summ.summary = {"summary": "Новое сжатие."}
    task = asyncio.create_task(
        compress_history(
            sessionmaker, KEYS, p.uid, p.conv, chat_models(summ, summ=summ), policy=policy
        )
    )
    for _ in range(200):
        if summ.kinds("summary"):
            break
        await asyncio.sleep(0.01)
    if order == "chat_reads_before_forget":
        assert await mem_ai.forget_memory(sessionmaker, KEYS, p.uid, item) is not None
    gate.set()
    res = await task
    model_in = summ.dump()
    if order == "chat_reads_after_forget":
        assert "СЕКРЕТa8" not in model_in  # прежняя сводка на очистке — модели не дана
    seen = await agent_sees(sessionmaker, p)
    assert res.status in ("saved", "conflict")
    assert "СЕКРЕТa8" not in seen, (order, res.status)


# =============================================================== 2. API /memory


@pytest.fixture
async def http(sessionmaker: SM) -> AsyncIterator[httpx.AsyncClient]:
    def settings() -> Settings:
        return Settings(
            telegram_bot_token=SecretStr("777005:A8-mem2-bot-token-not-real"),
            session_secret=SecretStr("a8-mem2-session-secret-" + "q" * 24),
            session_ttl_s=900, bot_username="A8_Mem2_bot", env="test",
        )  # fmt: skip

    saved = (app.state.sessionmaker, app.state.keyring, app.state.chat)
    app.state.sessionmaker, app.state.keyring = sessionmaker, KEYS
    app.dependency_overrides[get_settings] = settings
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://a8") as c:
        yield c
    app.dependency_overrides.clear()
    app.state.sessionmaker, app.state.keyring, app.state.chat = saved


async def _login(http: httpx.AsyncClient) -> tuple[uuid.UUID, dict[str, str]]:
    import os
    import time as _t

    from app.core.telegram import sign_init_data

    init = sign_init_data(
        {"auth_date": str(int(_t.time())),
         "user": json.dumps({"id": 770_000_000_000 + int.from_bytes(os.urandom(4), "big"),
                             "first_name": "Апи"})},
        "777005:A8-mem2-bot-token-not-real",
    )  # fmt: skip
    r = await http.post("/api/v1/auth/telegram", json={"init_data": init})
    assert r.status_code == 200
    return uuid.UUID(r.json()["me"]["id"]), {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.parametrize("models_ready", [True, False])
async def test_api_delete_cleans_or_hides(
    models_ready: bool, http: httpx.AsyncClient, engine: AsyncEngine, sessionmaker: SM,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """АР-37 через `DELETE /memory/{id}`: 204 сразу; модели готовы — фоновая очистка переписывает
    сводку; моделей нет — сводка скрыта до сжатия; в обоих случаях удалённого нет во входе агента;
    журнал без текстов и id."""
    caplog.set_level(logging.DEBUG)
    uid, h = await _login(http)
    async with engine.begin() as c:
        await c.execute(text("INSERT INTO agents (id, user_id) VALUES (gen_random_uuid(), :u)"),
                        {"u": uid})  # fmt: skip
        agent = (
            await c.execute(text("SELECT id FROM agents WHERE user_id = :u"), {"u": uid})
        ).scalar_one()
        conv = uuid.uuid4()
        await c.execute(text("INSERT INTO conversations (id, agent_id) VALUES (:c, :a)"),
                        {"c": conv, "a": agent})  # fmt: skip
        p = P(uid, agent, conv)
        item = await add_mem(c, p, SECRET)
        t1 = await say(c, p, "owner", "старое", ago(1))
        await say(c, p, "owner", "новое", ago(2))
    await put_summary(engine, p, f"{SECRET}. Любит чай.", t1)
    await put_profile(engine, p, SECRET)
    m = MemModel(profile="П")
    rt = ChatRuntime(models=models_of(m) if models_ready else None)
    app.state.chat = rt

    class Rw(MemModel):
        async def complete_json(self, **kw: Any) -> Any:
            if "rewritten" in kw["schema"]["properties"]:
                from app.ai.llm import StructuredResult, Usage

                return StructuredResult(data={"rewritten": "Любит чай."}, model="x",
                                        usage=Usage(), latency_s=0, cost_usd=0)  # fmt: skip
            return await super().complete_json(**kw)

    if models_ready:
        rt.models = models_of(Rw(profile="П"))
    r = await http.delete(f"/api/v1/memory/{item}", headers=h)
    assert r.status_code == 204
    assert "СЕКРЕТa8" not in await agent_sees(sessionmaker, p)
    await rt.drain()
    raw = await raw_summary(engine, p)
    assert raw is not None
    if models_ready:
        assert raw["text"] == "Любит чай." and "forget" not in raw
    else:
        assert raw["forget"] == [str(item)]
    assert "СЕКРЕТa8" not in await agent_sees(sessionmaker, p)
    server = "\n".join(x.getMessage() for x in caplog.records if not x.name.startswith("httpx"))
    assert "СЕКРЕТa8" not in server and str(item) not in server and str(uid) not in server


# =============================================================== 3. Захват, ФТ-14, вызовы, пачки


async def test_claim_does_not_lock_ongoing_sessions(engine: AsyncEngine) -> None:
    """АР-38 (MINOR-4): захват извлечения не блокирует агентов с идущей сессией — во время
    открытой транзакции захвата их строки свободны для `NOWAIT`; захваченный — занят."""
    async with engine.begin() as c:
        ready = await person(c, "Готов")
        await say(c, ready, "owner", "давно", ago(10))
        busy = await person(c, "Идёт")
        from datetime import UTC, datetime

        await say(c, busy, "owner", "прямо сейчас", datetime.now(UTC))
    await release_others(engine, ready)
    async with engine.begin() as c:  # «вторая» не занята чужой арендой
        await c.execute(text("UPDATE agents SET memory_lease_token = NULL, memory_lease_kind = NULL,"
                             " memory_lease_at = NULL, memory_lease_until = NULL WHERE id = :a"),
                        {"a": busy.agent})  # fmt: skip
    conn = await engine.connect()
    tx = await conn.begin()
    try:
        await conn.execute(text("SET LOCAL ROLE feelings_worker"))
        rows = (await conn.execute(text("SELECT agent_id FROM memory_claim_extraction(:g, "
                                        "interval '10 minutes', 100)"), {"g": GAP})).all()  # fmt: skip
        assert ready.agent in {r.agent_id for r in rows} and busy.agent not in {
            r.agent_id for r in rows
        }
        async with engine.begin() as c2:
            await c2.execute(text("SELECT 1 FROM agents WHERE id = :a FOR NO KEY UPDATE NOWAIT"),
                             {"a": busy.agent})  # fmt: skip
        with pytest.raises(DBAPIError) as e:
            async with engine.begin() as c3:
                await c3.execute(text("SELECT 1 FROM agents WHERE id = :a FOR NO KEY UPDATE NOWAIT"),
                                 {"a": ready.agent})  # fmt: skip
        assert e.value.orig.sqlstate == "55P03"  # type: ignore[union-attr]
    finally:
        await tx.rollback()
        await conn.close()


async def test_ft14_only_current_couple_in_claim_and_done(
    engine: AsyncEngine, sessionmaker: SM
) -> None:
    """АР-37/38 (ФТ-14): отметка `partner_through` захвата — только сообщения текущей активной
    пары; завершение с сообщением бывшей пары — false (откат)."""
    async with engine.begin() as c:
        anna = await person(c, "А14")
        igor = await person(c, "И14")
        ex = await person(c, "Б14")
        k, x = uuid.uuid4(), uuid.uuid4()
        await c.execute(text("INSERT INTO couples (id, status) VALUES (:k, 'active'), "
                             "(:x, 'dissolved')"), {"k": k, "x": x})  # fmt: skip
        await c.execute(
            text("INSERT INTO couple_members (couple_id, user_id, role, active) VALUES "
                 "(:x, :a, 'initiator', false), (:x, :e, 'partner', false), "
                 "(:k, :a, 'initiator', true), (:k, :i, 'partner', true)"),
            {"k": k, "x": x, "a": anna.uid, "i": igor.uid, "e": ex.uid},
        )  # fmt: skip
        ex_msg = await _pmsg(c, x, (ex.uid, ex.agent), anna.uid, "бывшее", "delivered", ago(5),
                             for_recipient="бывшее")  # fmt: skip
        cur = await _pmsg(c, k, (igor.uid, igor.agent), anna.uid, "текущее", "delivered",
                          ago(6), for_recipient="текущее")  # fmt: skip
    await release_others(engine, anna)
    claim = await claim_for(engine, anna)
    assert claim is not None and claim.partner_through[1] == cur
    async with user_scope(sessionmaker, anna.uid) as s:
        assert await mw.extraction_done_in(s, claim.token, None, ex_msg) is False


async def test_separate_calls_and_batches(engine: AsyncEngine, sessionmaker: SM) -> None:
    """Ревью MINOR-2/3: реплики владельца и сообщения партнёра — разными вызовами (в вызове
    владельца нет текстов партнёра и наоборот), источник задаёт код; длинная сессия — пачками
    ≤ 40 реплик и ≤ 8000 символов, каждая реплика ровно в одной пачке, длинная — обрезана."""
    async with engine.begin() as c:
        anna = await person(c, "Пачки")
        igor = await person(c, "ПачкиИ")
        k = uuid.uuid4()
        await c.execute(text("INSERT INTO couples (id, status) VALUES (:k, 'active')"), {"k": k})
        await c.execute(text("INSERT INTO couple_members (couple_id, user_id, role) VALUES "
                             "(:k, :a, 'initiator'), (:k, :i, 'partner')"),
                        {"k": k, "a": anna.uid, "i": igor.uid})  # fmt: skip
        for i in range(95):
            await say(c, anna, "owner", f"Р{i:03d} " + ("ш" * 9000 if i == 50 else "х" * 20),
                      ago(10) + timedelta(seconds=i))  # fmt: skip
        await _pmsg(c, k, (igor.uid, igor.agent), anna.uid, "ПАРТНЁРa8", "delivered", ago(8),
                    for_recipient="ПАРТНЁРa8")  # fmt: skip
    await release_others(engine, anna)

    def answer(payload: dict[str, Any]) -> list[dict[str, Any]]:
        if payload["partner_messages"]:
            return [{"text": "партнёр сказал", "kind": "fact",
                     "partner_ref": payload["partner_messages"][0]["ref"]}]  # fmt: skip
        return [{"text": "владелец сказал", "kind": "fact"}]

    m = MemModel(items=answer)
    claim = await claim_for(engine, anna)
    out = await mem_ai.run_extraction(sessionmaker, KEYS, FP, models_of(m), claim)
    assert out.status == "done"
    calls = m.of("extract")
    owner_calls = [c for c in calls if c["payload"]["owner_turns"]]
    partner_calls = [c for c in calls if c["payload"]["partner_messages"]]
    assert all(not c["payload"]["partner_messages"] for c in owner_calls)
    assert all(not c["payload"]["owner_turns"] for c in partner_calls)
    assert len(partner_calls) == 1 and "ПАРТНЁРa8" not in json.dumps(
        [c["payload"] for c in owner_calls], ensure_ascii=False
    )
    seen = [t for c in owner_calls for t in c["payload"]["owner_turns"]]
    assert sorted(t[:4] for t in seen) == [f"Р{i:03d}" for i in range(95)]
    for call in owner_calls:
        turns = call["payload"]["owner_turns"]
        assert len(turns) <= 40 and sum(len(t) for t in turns) <= 8000
    assert max(len(call["payload"]["owner_turns"]) for call in owner_calls) == 40
    rows = {r.text: r for r in await memory_of(engine, anna)}
    assert (rows["партнёр сказал"].type, rows["партнёр сказал"].source) == (
        "from_partner",
        "partner_message",
    )
    assert rows["владелец сказал"].source == "owner_dialog"


async def test_fingerprint_check_constraint(engine: AsyncEngine) -> None:
    """Б-68 (0010): текст без отпечатка — 23514 (вставка и обнуление отпечатка живой записи);
    удалённая запись (без текста) отпечаток хранит."""
    async with engine.begin() as c:
        p = await person(c, "CHECK")
        live = await add_mem(c, p, "x")
    for sql in (
        "INSERT INTO memory_items (agent_id, type, source, text_enc) VALUES (:a, 'fact', "
        "'owner_dialog', 'x')",
        "UPDATE memory_items SET fingerprint = NULL WHERE id = :i",
    ):
        with pytest.raises(DBAPIError) as e:
            async with engine.begin() as c:
                await c.execute(text(sql), {"a": p.agent, "i": live})
        assert e.value.orig.sqlstate == "23514", sql  # type: ignore[union-attr]
    async with engine.connect() as c:
        fp = (await c.execute(text("SELECT fingerprint FROM memory_items WHERE id = :i"),
                              {"i": live})).scalar_one()  # fmt: skip
    assert bytes(fp) == my_fp(FPK, p.agent, "x")

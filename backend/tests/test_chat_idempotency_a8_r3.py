"""2.2 р.3, A8 — исправления сервера чата в 0020 (коммит aac7896).

Негативные сценарии тестировщика к закрытию замечаний р.2: одно событие безопасности на реплику
(MINOR-N1) — в том числе без ключа, залпом повторов и при чужой/не той реплике; снятие аренды до
ответа (О-1) после 422 «под ключом другой текст»; ``ChatRuntime.drain`` не зависает на уже
завершённых задачах; откат 0020 при данных с ``safety_recorded`` (имена CHECK 0018 и 0020, D5),
CHECK запрещает отметку у ответа агента. ТЗ §7, §9.3, §10; DECISIONS.md АР-4, АР-34, АР-51;
карточка 2.2 (критерии 4–6). Модель — заглушки ``app.ai.stub``, БД — тестовый PostgreSQL,
приложение — под ролью входа ``feelings_api``. Данные выдуманные; тексты — с метками."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ai.llm import LLMUnavailable
from app.api.chat import ChatRuntime
from app.core.limits import MemoryDailyLimit
from app.repositories import chat as chat_repo
from tests.conftest import alembic
from tests.test_chat_api import API, Env, _error, _person, history, mark, models, stub
from tests.test_chat_idempotency import names, rows, send
from tests.test_chat_idempotency_a8 import BURST, _safety_count, _send
from tests.test_chat_idempotency_a8 import env as env  # фикстура A8 р.2

OWNER_CK = "ck_conversation_messages_owner_only_fields"
AGENT_CK = "ck_conversation_messages_agent_only_fields"
OWNER_CK_0018 = "ck_conversation_messages_ck_conversation_messages_owner_b5e5"
AGENT_CK_0018 = "ck_conversation_messages_ck_conversation_messages_agent_afa2"


async def _flags(engine: AsyncEngine, user_id: uuid.UUID) -> list[tuple[str, bool]]:
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT m.author::text, m.safety_recorded FROM conversation_messages m "
                "JOIN conversations c ON c.id = m.conversation_id "
                "JOIN agents a ON a.id = c.agent_id WHERE a.user_id = :u "
                "ORDER BY m.created_at, m.id"
            ),
            {"u": user_id},
        )
        return [(r[0], r[1]) for r in res]


async def _checks(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as c:
        res = await c.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE conrelid = "
                "'conversation_messages'::regclass AND contype = 'c' ORDER BY 1"
            )
        )
        return [r[0] for r in res]


# --------------------------------------------------------------------------- N1: одно событие


async def test_crisis_without_key_one_event_and_turn_marked(env: Env, engine: AsyncEngine) -> None:
    """N1, §9.3: кризис без ключа (старый клиент) — событие записано, реплика отмечена
    ``safety_recorded``; ответ протокола отметки не получает (CHECK). Следующая кризисная реплика
    без ключа — своё событие (это другая реплика, а не повтор)."""
    env.use(stub(category="self_harm"))
    anna = await _person(env.client)
    for i in range(2):
        resp = await env.client.post(API, headers=anna.headers, json={"text": mark(f"c{i}")})
        assert resp.status_code == 200 and "event: crisis" in resp.text
        await env.rt.drain()
        assert await _safety_count(engine, anna.id) == i + 1
    assert await _flags(engine, anna.id) == [
        ("owner", True),
        ("agent", False),
        ("owner", True),
        ("agent", False),
    ]


async def test_burst_repeats_of_unsaved_crisis_write_one_event(
    env: Env, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1 под нагрузкой (АР-51 «повтор без второго события»): ответ протокола не сохраняется,
    затем 8 одновременных повторов тем же ключом — у каждого 200 ``crisis`` с контактами или 409
    до потока, ни одного 429/ошибки; событий безопасности по реплике — ровно одно."""
    env.use(stub(category="self_harm", chunk_delay_s=0.02))
    anna = await _person(env.client)
    real = chat_repo.add_message

    async def no_crisis_save(*args: Any, **kw: Any) -> Any:
        if kw.get("crisis"):
            raise RuntimeError("db down")
        return await real(*args, **kw)

    monkeypatch.setattr(chat_repo, "add_message", no_crisis_save)
    key, body = uuid.uuid4(), mark("crisis-burst")
    _, first = await _send(env, anna, body, key)
    assert names(first) == ["accepted", "crisis"] and first[-1][1]["message_id"] is None
    assert await _safety_count(engine, anna.id) == 1
    res = await asyncio.gather(*(send(env, anna, body, key) for _ in range(BURST)))
    await env.rt.drain()
    codes = sorted(r.status_code for r, _ in res)
    assert set(codes) <= {200, 409} and 200 in codes, codes
    for resp, events in res:
        if resp.status_code == 200:
            assert names(events) == ["accepted", "crisis"] and events[-1][1]["contacts"]
        else:
            _error(resp, 409, "conflict_retry")
    assert await _safety_count(engine, anna.id) == 1
    assert sum(r["client_message_id"] == key for r in await rows(engine, anna.id)) == 1


async def test_safety_mark_of_other_owner_or_agent_row_is_refused(
    env: Env, engine: AsyncEngine
) -> None:
    """Чужой владелец (CLAUDE.md, слой доступа): ``record_safety_events`` с ``turn_id`` реплики
    Анны от имени Бориса — 0, у Анны отметка не ставится, событий ни у кого; ``turn_id`` ответа
    агента (не реплика владельца) — тоже 0. Своя реплика — 1, повтор — 0."""
    env.use(stub(chunks=[LLMUnavailable("down")]))
    anna, boris = await _person(env.client), await _person(env.client, "Борис", "Вера")
    env.use(stub())
    await _send(env, anna, mark("a"), uuid.uuid4())
    turn, reply = await rows(engine, anna.id)
    assert (turn["author"], reply["author"]) == ("owner", "agent")
    assert (
        await chat_repo.record_safety_events(env.sm, boris.id, ["self_harm"], turn_id=turn["id"])
        == 0
    )
    assert (
        await chat_repo.record_safety_events(env.sm, anna.id, ["self_harm"], turn_id=reply["id"])
        == 0
    )
    assert (
        await chat_repo.record_safety_events(env.sm, anna.id, ["self_harm"], turn_id=uuid.uuid4())
        == 0
    )
    assert await _flags(engine, anna.id) == [("owner", False), ("agent", False)]
    assert await _safety_count(engine, anna.id) == 0 and await _safety_count(engine, boris.id) == 0
    assert (
        await chat_repo.record_safety_events(env.sm, anna.id, ["self_harm"], turn_id=turn["id"])
        == 1
    )
    assert (
        await chat_repo.record_safety_events(env.sm, anna.id, ["self_harm"], turn_id=turn["id"])
        == 0
    )
    assert await _flags(engine, anna.id) == [("owner", True), ("agent", False)]
    assert await _safety_count(engine, anna.id) == 1 and await _safety_count(engine, boris.id) == 0


async def test_safety_event_text_free(env: Env, engine: AsyncEngine) -> None:
    """§9.3, CLAUDE.md «в safety_events нет текстов»: после кризиса с ключом в строках
    ``safety_events`` нет ни текста реплики, ни ключа."""
    env.use(stub(category="self_harm"))
    anna = await _person(env.client)
    key, body = uuid.uuid4(), mark("crisis-textfree")
    await _send(env, anna, body, key)
    async with engine.connect() as conn:
        dump = (
            await conn.execute(
                text(
                    "SELECT coalesce(string_agg(t::text, '|'), '') FROM safety_events t "
                    "WHERE user_id = :u"
                ),
                {"u": anna.id},
            )
        ).scalar_one()
    assert dump and body not in dump and str(key) not in dump


# --------------------------------------------------------------------------- О-1: аренда до ответа


async def test_immediate_repeat_after_key_mismatch_422_is_not_409(
    env: Env, engine: AsyncEngine
) -> None:
    """О-1, АР-51: реплика X под ключом сохранена без ответа (429); чужой текст под тем же ключом —
    422; сразу (без ожидания фоновых задач) повтор X — 200 с ответом на X, а не 409: ни 429, ни 422
    аренду не оставляют."""
    env.box.limit = 1
    anna = await _person(env.client)
    await send(env, anna, mark("first"), uuid.uuid4())
    key, x = uuid.uuid4(), mark("x")
    for _ in range(3):
        resp, _ = await send(env, anna, x, key)
        _error(resp, 429, "rate_limited")
        resp, _ = await send(env, anna, mark("other"), key)
        _error(resp, 422, "validation_error")
        assert all(r["reply_lease_until"] is None for r in await rows(engine, anna.id))
    env.box.limit = 5
    resp, events = await send(env, anna, x, key)
    assert resp.status_code == 200, resp.text
    assert names(events)[-1] == "done" and events[0][1]["replayed"] is True
    db = await rows(engine, anna.id)
    assert sum(r["client_message_id"] == key for r in db) == 1
    assert db[-1]["reply_to_id"] == next(r["id"] for r in db if r["client_message_id"] == key)


async def test_back_to_back_repeats_never_409(env: Env) -> None:
    """О-1: десять последовательных повторов того же ключа подряд, без ``drain`` — все 200 с тем
    же ответом (аренда снимается до конца потока); после ошибки модели — повтор сразу же 200."""
    anna = await _person(env.client)
    env.use(stub(chunks=[LLMUnavailable("down")]))
    key, body = uuid.uuid4(), mark("b2b")
    for _ in range(3):
        resp, events = await send(env, anna, body, key)
        assert resp.status_code == 200 and names(events)[-1] == "error"
    env.use(stub())
    _, answered = await send(env, anna, body, key)
    assert names(answered)[-1] == "done"
    for _ in range(10):
        resp, events = await send(env, anna, body, key)
        assert resp.status_code == 200 and names(events)[-1] == "done"
        assert events[-1][1]["message_id"] == answered[-1][1]["message_id"]


# --------------------------------------------------------------------------- drain


async def test_drain_returns_with_finished_and_late_tasks() -> None:
    """``ChatRuntime.drain`` (0020) не зависает: задачи уже завершены (их ``discard`` ещё в очереди
    цикла), задача порождает новую задачу и задача с исключением — ``drain`` возвращается, фоновых
    задач не остаётся."""
    rt = ChatRuntime(models=models(stub()), limiter=MemoryDailyLimit())

    async def noop() -> None:
        return None

    async def boom() -> None:
        raise RuntimeError("x")

    async def spawner() -> None:
        await asyncio.sleep(0.01)
        rt.spawn(noop())

    for _ in range(5):
        rt.spawn(noop())
    rt.spawn(boom())
    rt.spawn(spawner())
    await asyncio.wait_for(rt.drain(), 2)
    assert not rt.background
    t = rt.spawn(noop())
    await t  # завершена, ``discard`` ещё не выполнен
    await asyncio.wait_for(rt.drain(), 2)
    assert not rt.background


# --------------------------------------------------------------------------- 0020: CHECK и откат


async def test_agent_row_cannot_carry_safety_mark(engine: AsyncEngine) -> None:
    """0020: ``safety_recorded`` — только у реплики владельца (CHECK ``owner_only_fields``)."""
    async with engine.connect() as c:
        tx = await c.begin()
        try:
            row = (
                await c.execute(
                    text("SELECT id FROM conversation_messages WHERE author = 'agent' LIMIT 1")
                )
            ).first()
            if row is None:
                pytest.skip("нет ответа агента в БД")  # pragma: no cover
            with pytest.raises(IntegrityError, match=OWNER_CK):
                await c.execute(
                    text("UPDATE conversation_messages SET safety_recorded = true WHERE id = :i"),
                    {"i": row[0]},
                )
        finally:
            await tx.rollback()


async def test_downgrade_0020_keeps_rows_and_restores_0018_names(
    env: Env, engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """АР-4, D5: head → 0019 → head при отмеченных кризисных репликах. Строки, диалоги, время и
    шифротексты не меняются; на 0019 — колонки нет, CHECK под именами 0018 (как было), ограничения
    действуют; на голове — имена 0020, отметки сброшены (так описано в 0020), ``alembic check``
    чист, история читается, ответ на сохранённый кризис — повтор без нового события."""
    env.use(stub(category="self_harm"))
    anna = await _person(env.client)
    key, body = uuid.uuid4(), mark("crisis-down")
    _, first = await _send(env, anna, body, key)
    env.use(stub())
    await _send(env, anna, mark("normal"), uuid.uuid4())
    assert await _flags(engine, anna.id) == [
        ("owner", True),
        ("agent", False),
        ("owner", False),
        ("agent", False),
    ]
    dump = (
        "SELECT count(*), md5(string_agg(m.id::text || m.conversation_id::text || m.author::text "
        "|| m.created_at::text || encode(m.text_enc, 'hex') || coalesce(m.client_message_id::text, "
        "'') || coalesce(m.reply_to_id::text, '') || m.crisis::text || m.limit_charged::text, '|' "
        "ORDER BY m.id)) FROM conversation_messages m"
    )
    async with engine.connect() as c:
        before = (await c.execute(text(dump))).one()
    events = await _safety_count(engine, anna.id)
    try:
        alembic("downgrade", "0019")
        await engine.dispose()
        async with engine.connect() as c:
            assert (await c.execute(text(dump))).one() == before
            cols = (
                await c.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns WHERE table_name = "
                        "'conversation_messages' AND column_name = 'safety_recorded'"
                    )
                )
            ).scalar_one()
            assert cols == 0
        assert await _checks(engine) == [AGENT_CK_0018, OWNER_CK_0018]
        alembic("upgrade", "head")
        await engine.dispose()
        alembic("check")
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    async with engine.connect() as c:
        assert (await c.execute(text(dump))).one() == before
    assert await _checks(engine) == [AGENT_CK, OWNER_CK]
    assert all(not flag for _, flag in await _flags(engine, anna.id))
    items = await history(env, anna)
    assert len(items) == 4
    env.use(stub(category="self_harm"))
    _, again = await _send(env, anna, body, key)
    assert names(again) == ["accepted", "crisis"]
    assert again[-1][1]["message_id"] == first[-1][1]["message_id"]
    assert await _safety_count(engine, anna.id) == events

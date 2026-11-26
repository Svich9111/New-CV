"""2.2 р.2, A8 — ключ идемпотентности реплики (миграция 0018, ``POST /chat/messages``).

Негативные сценарии тестировщика к АР-51: повтор с ключом во всех состояниях под нагрузкой
(8 одновременных запросов с одним ключом), один ключ у двух владельцев, ключ из другого диалога
владельца, истечение аренды посреди ответа, кризис при повторе, лимит ровно на границе, мусорный
ключ, откат 0018 с данными. ТЗ §7, §9.3, §10; DECISIONS.md АР-24, АР-32, АР-34, АР-51;
карточка 2.2 (критерии 4–6). Модель — заглушки ``app.ai.stub``, БД — тестовый PostgreSQL,
приложение — под ролью входа ``feelings_api``. Данные выдуманные; тексты — с метками.

Дефекты — ``xfail(strict=True)`` с пометкой ``A8-2.2-Dn`` (docs/reviews/2.2-test-r2.md)."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ai.llm import LLMUnavailable
from app.ai.stub import json_chunks
from app.api.chat import ChatRuntime
from app.config import Settings, get_settings
from app.core.crypto import generate_key_entry, parse_keyring
from app.core.limits import MemoryDailyLimit
from app.main import app
from app.repositories import chat as chat_repo
from tests.conftest import alembic
from tests.test_auth_api import SECRET, TOKEN
from tests.test_chat_api import (
    ANSWER,
    API,
    SM,
    Box,
    Env,
    U,
    _error,
    _person,
    history,
    joined,
    mark,
    models,
    stub,
)
from tests.test_chat_idempotency import names, rows, send
from tests.test_couple_api import BOT

BURST = 8


@pytest.fixture
async def env(sessionmaker: SM) -> AsyncIterator[Env]:
    """Как ``tests.test_chat_idempotency.env``: приложение с заглушками и лимитом в памяти."""
    keys = parse_keyring(generate_key_entry("k1"), "k1")
    box = Box()
    rt = ChatRuntime(models=models(stub()), limiter=MemoryDailyLimit())

    def settings() -> Settings:
        return Settings(
            telegram_bot_token=SecretStr(TOKEN),
            session_secret=SecretStr(SECRET),
            session_ttl_s=600,
            bot_username=BOT,
            chat_daily_limit=box.limit,
            chat_keepalive_s=box.keepalive_s,
        )

    prev = (app.state.sessionmaker, app.state.keyring, app.state.chat)
    app.state.sessionmaker, app.state.keyring, app.state.chat = sessionmaker, keys, rt
    app.dependency_overrides[get_settings] = settings
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield Env(c, rt, box, keys, sessionmaker)
    await rt.drain()
    app.dependency_overrides.clear()
    app.state.sessionmaker, app.state.keyring, app.state.chat = prev


async def _burst(
    env: Env, who: list[tuple[U, str, uuid.UUID]]
) -> list[tuple[Any, list[tuple[str, Any]]]]:
    return list(await asyncio.gather(*(send(env, u, body, key) for u, body, key in who)))


async def _send(
    env: Env, user: U, body: str, key: uuid.UUID | None
) -> tuple[Any, list[tuple[str, Any]]]:
    """Последовательный запрос: после ответа — дождаться фоновых задач (снятие аренды идёт
    фоновой задачей; без ожидания немедленный повтор может застать её — замечание O-1)."""
    out = await send(env, user, body, key)
    await env.rt.drain()
    return out


def _statuses(results: list[tuple[Any, list[tuple[str, Any]]]]) -> list[int]:
    return sorted(r.status_code for r, _ in results)


async def _agent_rows(engine: AsyncEngine, user_id: uuid.UUID) -> list[dict[str, Any]]:
    return [r for r in await rows(engine, user_id) if r["author"] == "agent"]


async def _wait(cond: Any, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not await cond():
        assert time.monotonic() < deadline, "условие не наступило"
        await asyncio.sleep(0.02)


# --------------------------------------------------------------------------- состояния под нагрузкой


async def test_burst_new_key_one_turn_one_reply_one_charge(env: Env, engine: AsyncEngine) -> None:
    """АР-51 (а–в), АР-24: 8 одновременных запросов с новым ключом — ровно один поток (``done``),
    остальные — 409 ``conflict_retry`` до потока; одна реплика, один ответ, модель вызвана один
    раз, лимит засчитан один раз (лимит 2: после залпа проходит ещё ровно одна реплика)."""
    p = stub(chunk_delay_s=0.03)
    env.use(p)
    env.box.limit = 2
    anna = await _person(env.client)
    key, body = uuid.uuid4(), mark("burst")
    res = await _burst(env, [(anna, body, key)] * BURST)
    assert _statuses(res) == [200] + [409] * (BURST - 1)
    for resp, events in res:
        if resp.status_code == 409:
            _error(resp, 409, "conflict_retry")
            assert events == []
        else:
            assert names(events)[-1] == "done" and events[0][1]["replayed"] is False
    assert len(p.stream_requests) == 1
    await env.rt.drain()
    db = await rows(engine, anna.id)
    assert [r["author"] for r in db] == ["owner", "agent"]
    assert db[0]["limit_charged"] is True and db[0]["reply_lease_until"] is None
    assert db[1]["reply_to_id"] == db[0]["id"]
    resp, ok = await _send(env, anna, "вторая", uuid.uuid4())
    assert resp.status_code == 200 and names(ok)[-1] == "done"
    resp, _ = await _send(env, anna, "третья", uuid.uuid4())
    _error(resp, 429, "rate_limited")


async def test_burst_answered_key_all_replay_same_answer(env: Env, engine: AsyncEngine) -> None:
    """(а) Ответ сохранён: 8 одновременных повторов — все 200 с тем же ``accepted`` и тем же
    ответом (``replayed``), модель не вызывается, новых строк нет, лимит (1) не мешает."""
    env.use(stub())
    env.box.limit = 1
    anna = await _person(env.client)
    key, body = uuid.uuid4(), mark("answered")
    _, first = await _send(env, anna, body, key)
    p = stub()
    env.use(p)
    res = await _burst(env, [(anna, body, key)] * BURST)
    assert _statuses(res) == [200] * BURST
    for _, events in res:
        assert names(events) == ["accepted", "delta", "done"]
        assert events[0][1] == {**first[0][1], "replayed": True}
        assert events[-1][1]["message_id"] == first[-1][1]["message_id"]
        assert joined(events) == joined(first)
    assert p.requests == [] and p.stream_requests == []
    assert len(await rows(engine, anna.id)) == 2


async def test_burst_unanswered_key_one_answer(env: Env, engine: AsyncEngine) -> None:
    """(б) Реплика без ответа (модель упала): 8 одновременных повторов — один отвечает на ту же
    реплику (``replayed``), 7 — 409; ответ один, привязан к реплике; лимит не засчитан второй
    раз (лимит 1 исчерпан первой попыткой, но 429 нет)."""
    env.use(stub(chunks=[LLMUnavailable("down")]))
    env.box.limit = 1
    anna = await _person(env.client)
    key, body = uuid.uuid4(), mark("unanswered")
    _, first = await _send(env, anna, body, key)
    assert names(first) == ["accepted", "error"]
    p = stub(chunk_delay_s=0.03)
    env.use(p)
    res = await _burst(env, [(anna, body, key)] * BURST)
    assert _statuses(res) == [200] + [409] * (BURST - 1)
    ok = next(e for r, e in res if r.status_code == 200)
    assert names(ok)[-1] == "done" and ok[0][1] == {**first[0][1], "replayed": True}
    assert len(p.stream_requests) == 1
    await env.rt.drain()
    agents = await _agent_rows(engine, anna.id)
    assert len(agents) == 1 and agents[0]["reply_to_id"] == uuid.UUID(first[0][1]["message_id"])


async def test_burst_mixed_texts_under_one_key(env: Env, engine: AsyncEngine) -> None:
    """Под одним ключом одновременно два разных текста: сохраняется ровно одна реплика (того,
    кто первым взял блокировку агента); тот же текст — 409 (ответ готовится), другой — 422; после
    ответа — тот же текст получает сохранённый ответ, другой — по-прежнему 422."""
    env.use(stub(chunk_delay_s=0.03))
    anna = await _person(env.client)
    key, t1, t2 = uuid.uuid4(), mark("first"), mark("second")
    res = await _burst(env, [(anna, t1 if i % 2 else t2, key) for i in range(BURST)])
    owners = [i for i in await history(env, anna) if i["author"] == "owner"]
    assert len(owners) == 1
    stored = owners[0]["text"]
    bodies = [t1 if i % 2 else t2 for i in range(BURST)]
    winners = 0
    for body, (resp, events) in zip(bodies, res, strict=True):
        if resp.status_code == 200:
            winners += 1
            assert body == stored and names(events)[-1] == "done"
        elif body == stored:
            _error(resp, 409, "conflict_retry")
        else:
            _error(resp, 422, "validation_error")
    assert winners == 1
    await env.rt.drain()
    other = t2 if stored == t1 else t1
    resp, again = await _send(env, anna, stored, key)
    assert names(again) == ["accepted", "delta", "done"]
    resp, _ = await _send(env, anna, other, key)
    _error(resp, 422, "validation_error")
    assert len(await rows(engine, anna.id)) == 2


# --------------------------------------------------------------------------- два владельца, два диалога


async def test_one_key_two_owners_in_parallel(env: Env, engine: AsyncEngine) -> None:
    """Ключ уникален в пределах владельца (АР-51): Анна и Борис одновременно шлют по 4 запроса с
    одним и тем же ключом — у каждого ровно одна реплика и один ответ, блокировка одного не
    задерживает другого до 409; ответ Анны Борису не отдаётся, его ``accepted`` — его запись;
    реплика Анны с её текстом у Бориса — не 422 и не ответ Анны (оракула чужого ключа нет)."""
    env.use(stub(chunk_delay_s=0.03))
    anna, boris = await _person(env.client), await _person(env.client, "Борис", "Вера")
    key = uuid.uuid4()
    secret_a, text_b = mark("anna"), mark("boris")
    res = await _burst(env, [(anna, secret_a, key), (boris, text_b, key)] * 4)
    by_user: dict[str, list[int]] = {"a": [], "b": []}
    accepted: dict[str, str] = {}
    for i, (resp, events) in enumerate(res):
        who = "a" if i % 2 == 0 else "b"
        by_user[who].append(resp.status_code)
        if resp.status_code == 200:
            accepted[who] = events[0][1]["message_id"]
    assert sorted(by_user["a"]) == sorted(by_user["b"]) == [200, 409, 409, 409]
    assert accepted["a"] != accepted["b"]
    await env.rt.drain()
    hb = await history(env, boris)
    assert all(secret_a not in str(i["text"]) for i in hb)
    assert [i["id"] for i in hb if i["author"] == "owner"] == [accepted["b"]]
    # Борис повторяет с текстом Анны (догадался о тексте) — 422 по своему ключу, не ответ Анны.
    resp, _ = await _send(env, boris, secret_a, key)
    _error(resp, 422, "validation_error")
    # Третий человек с тем же ключом и текстом Анны — его новая реплика.
    olga = await _person(env.client, "Ольга", "Пётр")
    _, events = await _send(env, olga, secret_a, key)
    assert events[0][1]["replayed"] is False
    assert events[0][1]["message_id"] not in (accepted["a"], accepted["b"])
    ha = await history(env, anna)
    assert len(ha) == 2 and len(await history(env, olga)) == 2


async def _new_conversation(engine: AsyncEngine, user_id: uuid.UUID) -> uuid.UUID:
    async with engine.begin() as conn:
        conv = (
            await conn.execute(
                text(
                    "INSERT INTO conversations (agent_id, started_at) "
                    "SELECT a.id, now() + interval '1 second' FROM agents a WHERE a.user_id = :u "
                    "RETURNING id"
                ),
                {"u": user_id},
            )
        ).scalar_one()
    return uuid.UUID(str(conv))


async def _conv_of(engine: AsyncEngine, message_id: uuid.UUID | str) -> uuid.UUID:
    async with engine.connect() as conn:
        return uuid.UUID(
            str(
                (
                    await conn.execute(
                        text("SELECT conversation_id FROM conversation_messages WHERE id = :i"),
                        {"i": uuid.UUID(str(message_id))},
                    )
                ).scalar_one()
            )
        )


async def test_key_from_other_dialog_of_owner(env: Env, engine: AsyncEngine) -> None:
    """Ключ ищется по **всем** диалогам владельца (АР-51): реплика без ответа в прежнем диалоге,
    у владельца появился новый (текущий) диалог — повтор с тем же ключом отвечает на ту же реплику
    в её диалоге (новой реплики в текущем нет), другой текст — 422, ответ сохранён — повтор
    отдаёт его; новый ключ уходит в текущий диалог."""
    env.use(stub(chunks=[LLMUnavailable("down")]))
    anna = await _person(env.client)
    key, body = uuid.uuid4(), mark("old-dialog")
    _, first = await _send(env, anna, body, key)
    turn = first[0][1]["message_id"]
    old = await _conv_of(engine, turn)
    new = await _new_conversation(engine, anna.id)
    p = stub()
    env.use(p)
    resp, _ = await _send(env, anna, mark("other"), key)
    _error(resp, 422, "validation_error")
    _, again = await _send(env, anna, body, key)
    assert names(again)[-1] == "done"
    assert again[0][1] == {**first[0][1], "replayed": True}
    reply = again[-1][1]["message_id"]
    assert await _conv_of(engine, reply) == old
    assert body in p.stream_requests[0]["messages"][-1][1]
    _, replay = await _send(env, anna, body, key)
    assert names(replay) == ["accepted", "delta", "done"]
    assert replay[-1][1]["message_id"] == reply
    _, fresh = await _send(env, anna, mark("fresh"), uuid.uuid4())
    assert await _conv_of(engine, fresh[0][1]["message_id"]) == new
    async with engine.connect() as conn:
        n_new = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM conversation_messages WHERE conversation_id = :c "
                    "AND client_message_id = :k"
                ),
                {"c": new, "k": key},
            )
        ).scalar_one()
    assert n_new == 0
    owners = [i for i in await history(env, anna) if i["author"] == "owner"]
    assert sum(i["client_message_id"] == str(key) for i in owners) == 1


# --------------------------------------------------------------------------- аренда


async def _lease(engine: AsyncEngine, message_id: str) -> Any:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT reply_lease_until, reply_lease_until > clock_timestamp() AS live "
                    "FROM conversation_messages WHERE id = :i"
                ),
                {"i": uuid.UUID(message_id)},
            )
        ).one()


async def test_lease_expires_mid_answer_one_reply_saved(env: Env, engine: AsyncEngine) -> None:
    """Аренда истекла, пока первый запрос ещё отвечает (медленная модель, срок аренды короче
    ответа): второй запрос с тем же ключом берёт аренду и отвечает. Ответов на реплику в БД —
    ровно один (индекс 0018); проигравший поток не сообщает ``done`` с id, которого нет; дальше
    повтор отдаёт сохранённый ответ."""
    env.rt.reply_lease_s = 0.3
    slow = stub(chunks=json_chunks(ANSWER, 12), chunk_delay_s=0.1)
    env.use(slow)
    anna = await _person(env.client)
    key, body = uuid.uuid4(), mark("lease")
    a = asyncio.create_task(send(env, anna, body, key))

    async def turn_saved() -> bool:
        return bool(await rows(engine, anna.id))

    await _wait(turn_saved)
    turn = str((await rows(engine, anna.id))[0]["id"])

    async def expired() -> bool:
        return not (await _lease(engine, turn)).live

    await _wait(expired)
    assert not a.done()
    env.use(stub())
    resp_b, b = await _send(env, anna, body, key)
    assert resp_b.status_code == 200 and names(b)[-1] == "done" and b[0][1]["replayed"] is True
    resp_a, ev_a = await a
    assert resp_a.status_code == 200
    await env.rt.drain()
    agents = await _agent_rows(engine, anna.id)
    assert len(agents) == 1 and agents[0]["reply_to_id"] == uuid.UUID(turn)
    saved = str(agents[0]["id"])
    assert b[-1][1]["message_id"] == saved
    last_a = ev_a[-1]
    assert last_a[0] in ("done", "error"), names(ev_a)
    if last_a[0] == "done":
        assert last_a[1]["message_id"] == saved
    _, replay = await _send(env, anna, body, key)
    assert names(replay) == ["accepted", "delta", "done"]
    assert replay[-1][1]["message_id"] == saved


async def test_expired_holder_does_not_release_new_lease(env: Env, engine: AsyncEngine) -> None:
    """Аренду, взятую после истечения другим запросом, прежний владелец не снимает (сравнение по
    значению): первый запрос упал после истечения своей аренды — аренда второго (ещё отвечает)
    остаётся, третий запрос — 409, а не второй ответ."""
    env.rt.reply_lease_s = 0.3
    first = stub(chunks=[*json_chunks(ANSWER, 40)[:3], LLMUnavailable("down")], chunk_delay_s=0.25)
    env.use(first)
    anna = await _person(env.client)
    key, body = uuid.uuid4(), mark("holder")
    a = asyncio.create_task(send(env, anna, body, key))

    async def turn_saved() -> bool:
        return bool(await rows(engine, anna.id))

    await _wait(turn_saved)
    turn = str((await rows(engine, anna.id))[0]["id"])

    async def expired() -> bool:
        return not (await _lease(engine, turn)).live

    await _wait(expired)
    env.rt.reply_lease_s = 30.0
    second = stub(chunks=json_chunks(ANSWER, 6), chunk_delay_s=0.15)
    env.use(second)
    b = asyncio.create_task(send(env, anna, body, key))

    async def renewed() -> bool:
        return bool((await _lease(engine, turn)).live)

    await _wait(renewed)
    lease_b = (await _lease(engine, turn)).reply_lease_until
    _, ev_a = await a
    assert names(ev_a)[-1] == "error"
    await asyncio.sleep(0.2)  # фоновое снятие аренды первого запроса успело бы сработать
    assert not b.done()
    now = await _lease(engine, turn)
    assert now.reply_lease_until == lease_b and now.live
    resp_c, _ = await _send(env, anna, body, key)
    _error(resp_c, 409, "conflict_retry")
    _, ev_b = await b
    assert names(ev_b)[-1] == "done"
    await env.rt.drain()
    assert len(await _agent_rows(engine, anna.id)) == 1
    assert (await _lease(engine, turn)).reply_lease_until is None


# --------------------------------------------------------------------------- кризис при повторе


async def _safety_count(engine: AsyncEngine, user_id: uuid.UUID) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM safety_events WHERE user_id = :u"), {"u": user_id}
                )
            ).scalar_one()
        )


async def test_crisis_on_repeat_after_429_is_not_blocked_by_limit(
    env: Env, engine: AsyncEngine
) -> None:
    """§9.3, §11 п.5, АР-34 п.1: реплика с ключом получила 429 (не засчитана), лимит всё ещё
    исчерпан; при повторе классификатор видит кризис — ответ протокола идёт сверх лимита, ответ
    сохранён с ``crisis`` и ``reply_to`` на ту же реплику (дубля нет); следующий повтор — тот же
    ``crisis`` (``replace: false``) без нового события безопасности."""
    env.box.limit = 1
    anna = await _person(env.client)
    await _send(env, anna, "обычная", uuid.uuid4())
    key, body = uuid.uuid4(), mark("crisis-after-429")
    resp, _ = await _send(env, anna, body, key)
    _error(resp, 429, "rate_limited")
    # Аренда после 429 снимается фоновой задачей: повтор сразу же может получить 409 (замечание
    # O-1 отчёта); здесь ждём её, как ждёт пауза клиента.
    await env.rt.drain()
    db = await rows(engine, anna.id)
    turn = db[-1]
    assert turn["client_message_id"] == key and turn["limit_charged"] is False
    env.use(stub(category="self_harm"))
    _, crisis = await _send(env, anna, body, key)
    assert names(crisis) == ["accepted", "crisis"]
    assert crisis[0][1]["message_id"] == str(turn["id"]) and crisis[0][1]["replayed"] is True
    await env.rt.drain()
    db = await rows(engine, anna.id)
    reply = db[-1]
    assert reply["author"] == "agent" and reply["crisis"] is True
    assert reply["reply_to_id"] == turn["id"]
    assert sum(r["client_message_id"] == key for r in db) == 1
    assert next(r for r in db if r["id"] == turn["id"])["limit_charged"] is True
    events = await _safety_count(engine, anna.id)
    assert events == 1
    _, again = await _send(env, anna, body, key)
    assert names(again) == ["accepted", "crisis"]
    assert again[-1][1] == {**crisis[-1][1], "replace": False}
    assert again[-1][1]["contacts"]
    await env.rt.drain()
    assert await _safety_count(engine, anna.id) == events


async def test_crisis_reply_not_saved_then_repeat_saves_it(
    env: Env, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Кризис, ответ протокола сохранить не удалось: событие ``crisis`` с контактами всё равно
    идёт (``message_id: null``, §9.3). Реплика осталась без ответа — повтор с тем же ключом снова
    отвечает протоколом и сохраняет ответ (``crisis: true``, ``reply_to``) — в истории кризис
    виден и после нового открытия чата (карточка 2.2, критерий 5)."""
    env.use(stub(category="self_harm"))
    anna = await _person(env.client)
    real = chat_repo.add_message

    async def no_crisis_save(*args: Any, **kw: Any) -> Any:
        if kw.get("crisis"):
            raise RuntimeError("db down")
        return await real(*args, **kw)

    monkeypatch.setattr(chat_repo, "add_message", no_crisis_save)
    key, body = uuid.uuid4(), mark("crisis-unsaved")
    _, first = await _send(env, anna, body, key)
    assert names(first) == ["accepted", "crisis"]
    assert first[-1][1]["message_id"] is None and first[-1][1]["contacts"]
    monkeypatch.undo()
    await env.rt.drain()
    _, again = await _send(env, anna, body, key)
    assert names(again) == ["accepted", "crisis"]
    assert again[0][1] == {**first[0][1], "replayed": True}
    saved = again[-1][1]["message_id"]
    assert saved is not None
    items = await history(env, anna)
    assert [(i["author"], i["crisis"]) for i in items] == [("agent", True), ("owner", False)]
    assert items[0]["id"] == saved and items[0]["reply_to"] == first[0][1]["message_id"]


async def test_normal_answer_is_replayed_even_if_classifier_now_says_crisis(env: Env) -> None:
    """Повтор с сохранённым ответом классификатор не вызывает (АР-51 (а)): ответ — тот же
    ``done``; кризисный протокол на повтор не накладывается и не подменяет сохранённый текст."""
    env.use(stub())
    anna = await _person(env.client)
    key, body = uuid.uuid4(), mark("normal")
    _, first = await _send(env, anna, body, key)
    p = stub(category="self_harm")
    env.use(p)
    _, again = await _send(env, anna, body, key)
    assert names(again) == ["accepted", "delta", "done"]
    assert joined(again) == joined(first)
    assert p.requests == []


# --------------------------------------------------------------------------- лимит на границе


async def test_limit_exactly_at_boundary_with_keys(env: Env, engine: AsyncEngine) -> None:
    """АР-32, АР-51: лимит 3. Две реплики, третья — ошибка модели (засчитана): ровно на границе
    повтор третьей отвечает (не 429), новая — 429 и не засчитана; её повтор — снова 429; новое
    окно (лимит 4) — повтор отвечает на ту же реплику, следующая новая — 429; без ключа на
    границе — тоже 429."""
    env.box.limit = 3
    anna = await _person(env.client)
    for n in range(2):
        resp, ev = await _send(env, anna, f"реплика {n}", uuid.uuid4())
        assert names(ev)[-1] == "done"
    env.use(stub(chunks=[LLMUnavailable("down")]))
    k3 = uuid.uuid4()
    _, e3 = await _send(env, anna, "третья", k3)
    assert names(e3) == ["accepted", "error"]
    env.use(stub())
    _, r3 = await _send(env, anna, "третья", k3)
    assert names(r3)[-1] == "done" and r3[0][1]["replayed"] is True
    k4 = uuid.uuid4()
    for _ in range(2):
        resp, _ = await _send(env, anna, "четвёртая", k4)
        _error(resp, 429, "rate_limited")
        assert int(resp.headers["retry-after"]) >= 1
    resp = await env.client.post(API, headers=anna.headers, json={"text": "без ключа"})
    _error(resp, 429, "rate_limited")
    db = await rows(engine, anna.id)
    k4_rows = [r for r in db if r["client_message_id"] == k4]
    assert len(k4_rows) == 1 and k4_rows[0]["limit_charged"] is False
    env.box.limit = 4
    _, r4 = await _send(env, anna, "четвёртая", k4)
    assert names(r4)[-1] == "done" and r4[0][1]["message_id"] == str(k4_rows[0]["id"])
    resp, _ = await _send(env, anna, "пятая", uuid.uuid4())
    _error(resp, 429, "rate_limited")
    _, r4b = await _send(env, anna, "четвёртая", k4)
    assert names(r4b) == ["accepted", "delta", "done"]


async def test_limit_boundary_parallel_distinct_keys(env: Env, engine: AsyncEngine) -> None:
    """Последнее место в лимите и 6 одновременных реплик с разными ключами: ровно одна
    отвечает, остальные — 429; засчитана одна (``limit_charged`` у одной реплики)."""
    env.use(stub(chunk_delay_s=0.01))
    env.box.limit = 2
    anna = await _person(env.client)
    await _send(env, anna, "первая", uuid.uuid4())
    keys = [uuid.uuid4() for _ in range(6)]
    res = await _burst(env, [(anna, f"залп {i}", k) for i, k in enumerate(keys)])
    assert _statuses(res) == [200] + [429] * 5
    await env.rt.drain()
    db = await rows(engine, anna.id)
    charged = [r for r in db if r["client_message_id"] in keys and r["limit_charged"]]
    assert len(charged) == 1
    assert len([r for r in db if r["author"] == "agent"]) == 2


# --------------------------------------------------------------------------- мусорный ключ


GARBAGE: list[Any] = [
    "",
    " ",
    "не-uuid",
    "'; DROP TABLE conversation_messages; --",
    "0" * 35 + "g",
    "x" * 5000,
    "123e4567-e89b-12d3-a456-42661417400",  # на символ короче
    "123e4567-e89b-12d3-a456-4266141740000",  # на символ длиннее
    " 123e4567-e89b-12d3-a456-426614174000",
    "123e4567-e89b-12d3-a456-426614174000\n",
    0,
    1.5,
    True,
    [],
    ["123e4567-e89b-12d3-a456-426614174000"],
    {"id": "123e4567-e89b-12d3-a456-426614174000"},
]


@pytest.mark.parametrize("bad", GARBAGE, ids=[f"g{i}" for i in range(len(GARBAGE))])
async def test_garbage_key_is_422_and_nothing_saved(
    env: Env, engine: AsyncEngine, bad: Any
) -> None:
    """Ключ не uuid — 422 ``validation_error`` по схеме ``Error``; модель не вызывается, реплика
    не сохраняется, лимит не тратится."""
    p = stub()
    env.use(p)
    env.box.limit = 1
    anna = await _person(env.client)
    resp = await env.client.post(
        API, headers=anna.headers, json={"text": "Привет", "client_message_id": bad}
    )
    _error(resp, 422, "validation_error")
    assert p.requests == [] and p.stream_requests == []
    assert await rows(engine, anna.id) == []
    _, ok = await _send(env, anna, "Привет", uuid.uuid4())
    assert names(ok)[-1] == "done"


async def test_key_spellings_are_one_key(env: Env, engine: AsyncEngine) -> None:
    """Сервер принимает и другие записи uuid (верхний регистр, без дефисов, ``{…}``, ``urn:uuid:``)
    и приводит их к одной: это тот же ключ — повтор, а не новая реплика; в ``accepted`` и истории —
    каноническая запись (строчная, с дефисами). Нулевой uuid — обычный ключ владельца."""
    env.use(stub())
    anna = await _person(env.client)
    key = uuid.uuid4()
    canon = str(key)
    _, first = await _send(env, anna, "Привет", key)
    for spelling in (canon.upper(), key.hex, "{" + canon + "}", "urn:uuid:" + canon):
        resp = await env.client.post(
            API, headers=anna.headers, json={"text": "Привет", "client_message_id": spelling}
        )
        assert resp.status_code == 200, spelling
        assert '"replayed":true' in resp.text and f'"client_message_id":"{canon}"' in resp.text
    assert len(await rows(engine, anna.id)) == 2
    nil = uuid.UUID(int=0)
    _, e = await _send(env, anna, "Ноль", nil)
    assert e[0][1]["client_message_id"] == str(nil) and e[0][1]["replayed"] is False
    olga = await _person(env.client, "Ольга", "Пётр")
    _, e2 = await _send(env, olga, "Ноль", nil)
    assert e2[0][1]["replayed"] is False and e2[0][1]["message_id"] != e[0][1]["message_id"]
    assert first[0][1]["client_message_id"] == canon


# --------------------------------------------------------------------------- откат 0018 с данными

# Ограничения 0018 — по смыслу, а не по имени (имена CHECK искажены соглашением, A8-2.2-D5):
# два CHECK по новым колонкам, составной FK ответа и уникальность (conversation_id, id).
CONSTRAINTS_0018 = (
    "SELECT count(*) FROM pg_constraint WHERE conrelid = 'conversation_messages'::regclass AND ("
    "(contype = 'c' AND (pg_get_constraintdef(oid) LIKE '%limit_charged%' "
    "OR pg_get_constraintdef(oid) LIKE '%reply_to_id%')) "
    "OR (contype = 'f' AND pg_get_constraintdef(oid) LIKE '%reply_to_id%') "
    "OR (contype = 'u' AND pg_get_constraintdef(oid) = 'UNIQUE (conversation_id, id)'))"
)


# A8-2.2-D5 исправлен в 0020 (2.2 р.2, A1): xfail снят, тело не менялось.
async def test_0018_check_constraint_names(engine: AsyncEngine) -> None:
    """Имена CHECK 0018 — как в описании миграции и в тесте A1 (``ck_conversation_messages_
    owner_only_fields``/``…_agent_only_fields``), как у CHECK 0017 (``ck_payments_…``)."""
    async with engine.connect() as c:
        got = (
            await c.execute(
                text(
                    "SELECT string_agg(conname, ',' ORDER BY conname) FROM pg_constraint "
                    "WHERE conrelid = 'conversation_messages'::regclass AND contype = 'c'"
                )
            )
        ).scalar_one()
    assert got == (
        "ck_conversation_messages_agent_only_fields,ck_conversation_messages_owner_only_fields"
    )


async def test_downgrade_0018_with_rich_data_keeps_rows(
    env: Env, engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """head → 0017 → head при данных 0018 всех видов: реплика с ключом и ответом, реплика без
    ответа с действующей арендой, кризисный ответ, повтор после более поздних реплик (ответ не
    по порядку), реплика без ключа, второй владелец. Все строки, их диалоги, время и шифротексты
    сохраняются; после возврата — данные 0018 пусты (ключи, связи, флаги, аренды), CHECK и
    индексы снова на месте, история читается, ``alembic check`` чист."""
    env.use(stub())
    anna, boris = await _person(env.client), await _person(env.client, "Борис", "Вера")
    await _send(env, anna, mark("keyed"), uuid.uuid4())
    env.use(stub(chunks=[LLMUnavailable("down")]))
    kx, x = uuid.uuid4(), mark("x")
    await _send(env, anna, x, kx)
    env.use(stub())
    await _send(env, anna, mark("later"), uuid.uuid4())
    await _send(env, anna, x, kx)  # ответ на X после более поздней реплики
    await env.client.post(API, headers=anna.headers, json={"text": mark("nokey")})
    env.use(stub(category="self_harm"))
    await _send(env, anna, mark("crisis"), uuid.uuid4())
    env.use(stub(chunks=[LLMUnavailable("down")]))
    kb = uuid.uuid4()
    await _send(env, boris, mark("boris"), kb)
    await env.rt.drain()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE conversation_messages SET reply_lease_until = now() + interval '1 hour' "
                "WHERE client_message_id = :k"
            ),
            {"k": kb},
        )
    dump = (
        "SELECT count(*), md5(string_agg(m.id::text || m.conversation_id::text || m.author::text "
        "|| m.created_at::text || encode(m.text_enc, 'hex'), '|' ORDER BY m.id)) "
        "FROM conversation_messages m"
    )
    async with engine.connect() as c:
        before = (await c.execute(text(dump))).one()
        flagged = (
            await c.execute(
                text(
                    "SELECT count(*) FILTER (WHERE crisis), count(reply_to_id), "
                    "count(client_message_id), count(reply_lease_until) "
                    "FROM conversation_messages"
                )
            )
        ).one()
    assert all(n >= 1 for n in flagged), flagged
    try:
        alembic("downgrade", "0017")
        await engine.dispose()
        async with engine.connect() as c:
            assert (await c.execute(text(dump))).one() == before
            idx = (
                await c.execute(
                    text(
                        "SELECT count(*) FROM pg_indexes WHERE tablename = 'conversation_messages' "
                        "AND indexname IN ('uq_conversation_messages_client_message_id', "
                        "'uq_conversation_messages_reply_to_id')"
                    )
                )
            ).scalar_one()
            assert idx == 0
            assert (await c.execute(text(CONSTRAINTS_0018))).scalar_one() == 0
        alembic("upgrade", "head")
        await engine.dispose()
        alembic("check")
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    async with engine.connect() as c:
        assert (await c.execute(text(dump))).one() == before
        after = (
            await c.execute(
                text(
                    "SELECT count(*) FILTER (WHERE crisis), count(reply_to_id), "
                    "count(client_message_id), count(reply_lease_until), "
                    "count(*) FILTER (WHERE limit_charged) FROM conversation_messages"
                )
            )
        ).one()
        cons = (await c.execute(text(CONSTRAINTS_0018))).scalar_one()
    assert tuple(after) == (0, 0, 0, 0, 0) and cons == 4
    items = await history(env, anna)
    assert len(items) == 10 and all(i["reply_to"] is None and not i["crisis"] for i in items)
    # Аренда Бориса пропала вместе с колонкой: повтор с прежним ключом — новая реплика (ключ
    # 0018 не восстанавливается — АР-51, данные отката).
    env.use(stub())
    _, again = await _send(env, boris, mark("boris-again"), kb)
    assert again[0][1]["replayed"] is False and names(again)[-1] == "done"


@pytest.mark.parametrize("body", ["\x1f", "\x1c\x1d", " \x85 "])
async def test_control_whitespace_text_is_422(env: Env, engine: AsyncEngine, body: str) -> None:
    """К A8-2.2-D6 (клиент): реплика из разделителей U+001C…U+001F / U+0085 сервер считает пустой
    (``str.strip``) — 422 ``validation_error`` и с ключом, и без; ничего не сохраняется. Клиент такую
    реплику отправляет (``String.prototype.trim`` их не убирает) и показывает текст о ключе."""
    anna = await _person(env.client)
    for key in (uuid.uuid4(), None):
        resp, _ = await send(env, anna, body, key)
        _error(resp, 422, "validation_error")
    assert await rows(engine, anna.id) == []

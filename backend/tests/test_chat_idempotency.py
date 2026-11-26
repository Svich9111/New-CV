"""2.2, исправления р.1 (A1): ключ идемпотентности `client_message_id` в `POST /chat/messages`,
событие `accepted`, связь ответа с репликой и флаг кризиса в истории (миграция 0018).

Ревью 2.2 р.1 MAJOR-1, MAJOR-2, MINOR-3, MINOR-4; предложения A4 1–3 (docs/tasks/2.2.md);
DECISIONS.md АР-24, АР-32, АР-34, АР-51; ТЗ §9.3, §10. Модель — заглушки `app.ai.stub`, БД —
тестовый PostgreSQL с миграциями, приложение — под ролью входа `feelings_api`. Данные выдуманные;
тексты помечены уникальными метками — утечки ищутся поиском меток."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, MutableMapping, Sequence
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.ai.llm import LLMUnavailable
from app.ai.stub import json_chunks
from app.api.chat import ChatRuntime
from app.config import Settings, get_settings
from app.core.crypto import generate_key_entry, parse_keyring
from app.core.limits import MemoryDailyLimit
from app.db import user_scope
from app.main import app
from app.repositories import chat as chat_repo
from tests.conftest import alembic
from tests.contract import assert_matches
from tests.test_auth_api import SECRET, TOKEN
from tests.test_chat_api import (
    ANSWER,
    API,
    EVENT_SCHEMAS,
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
    parse_sse,
    stub,
)
from tests.test_couple_api import BOT

SCHEMAS = {**EVENT_SCHEMAS, "accepted": "ChatStreamAccepted"}


@pytest.fixture
async def env(sessionmaker: SM) -> AsyncIterator[Env]:
    """Как ``tests.test_chat_api.env``: приложение с заглушками моделей и лимитом в памяти."""
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


def parse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Поток по контракту с ключом: `accepted` первым, `delta`*, ровно одно завершающее."""
    assert body.endswith("\n\n"), body[-40:]
    events: list[tuple[str, dict[str, Any]]] = []
    for block in body.split("\n\n")[:-1]:
        lines = block.split("\n")
        if all(line.startswith(":") for line in lines):
            continue
        assert len(lines) == 2, block
        name, data = lines[0][len("event: ") :], json.loads(lines[1][len("data: ") :])
        assert_matches(data, SCHEMAS[name])
        events.append((name, data))
    names = [n for n, _ in events]
    assert names[0] == "accepted", names
    assert names[-1] in ("done", "crisis", "error"), names
    assert all(n == "delta" for n in names[1:-1]), names
    return events


async def send(
    env: Env, user: U, body: str, key: uuid.UUID | None
) -> tuple[httpx.Response, list[tuple[str, Any]]]:
    payload: dict[str, Any] = {"text": body}
    if key is not None:
        payload["client_message_id"] = str(key)
    resp = await env.client.post(API, headers=user.headers, json=payload)
    if resp.status_code != 200:
        return resp, []
    assert resp.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert resp.headers["cache-control"] == "no-store"
    if key is None:
        return resp, parse_sse(resp.text)
    return resp, parse(resp.text)


async def rows(engine: AsyncEngine, user_id: uuid.UUID) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT m.id, m.author::text AS author, m.client_message_id, m.reply_to_id, "
                "m.crisis, m.reply_lease_until, m.limit_charged FROM conversation_messages m "
                "JOIN conversations c ON c.id = m.conversation_id "
                "JOIN agents a ON a.id = c.agent_id WHERE a.user_id = :u "
                "ORDER BY m.created_at, m.id"
            ),
            {"u": user_id},
        )
        return [dict(r) for r in res.mappings()]


def names(events: Sequence[tuple[str, Any]]) -> list[str]:
    return [n for n, _ in events]


# --------------------------------------------------------------------------- без ключа и accepted


async def test_without_key_stream_and_rows_as_before(env: Env, engine: AsyncEngine) -> None:
    """Без ключа — как раньше: `accepted` нет, `delta`* + `done`; в истории новые поля пусты, у
    ответа — связь с репликой."""
    env.use(stub())
    anna = await _person(env.client)
    resp, events = await send(env, anna, "Привет", None)
    assert resp.status_code == 200 and names(events)[0] == "delta" and names(events)[-1] == "done"
    items = await history(env, anna)
    for item in items:
        assert_matches(item, "ChatMessage")
    agent, owner = items
    assert (owner["client_message_id"], owner["reply_to"], owner["crisis"]) == (None, None, False)
    assert (agent["client_message_id"], agent["reply_to"], agent["crisis"]) == (
        None,
        owner["id"],
        False,
    )
    assert all(
        r["reply_lease_until"] is None and not r["limit_charged"]
        for r in await rows(engine, anna.id)
    )
    # Ключ `null` — то же, что без ключа.
    resp = await env.client.post(
        API, headers=anna.headers, json={"text": "Ещё", "client_message_id": None}
    )
    assert resp.status_code == 200 and "event: accepted" not in resp.text


async def test_accepted_is_first_event_with_owner_message_id(env: Env, engine: AsyncEngine) -> None:
    """С ключом первое событие — `accepted` с id сохранённой реплики и ключом (сверка по id,
    MINOR-4); в истории у реплики — ключ, у ответа — `reply_to`; аренда снята, лимит отмечен."""
    env.use(stub())
    anna = await _person(env.client)
    key = uuid.uuid4()
    _, events = await send(env, anna, "Привет", key)
    assert names(events)[-1] == "done" and len(events) > 2
    accepted = events[0][1]
    assert accepted["client_message_id"] == str(key) and accepted["replayed"] is False
    agent, owner = await history(env, anna)
    assert owner["id"] == accepted["message_id"] and owner["client_message_id"] == str(key)
    assert agent["reply_to"] == owner["id"] and agent["id"] == events[-1][1]["message_id"]
    assert agent["client_message_id"] is None and agent["crisis"] is False
    await env.rt.drain()
    db = await rows(engine, anna.id)
    assert db[0]["reply_lease_until"] is None and db[0]["limit_charged"] is True


@pytest.mark.parametrize("bad", ["не-uuid", 123, ""])
async def test_key_must_be_uuid(env: Env, bad: Any) -> None:
    anna = await _person(env.client)
    resp = await env.client.post(
        API, headers=anna.headers, json={"text": "Привет", "client_message_id": bad}
    )
    _error(resp, 422, "validation_error")
    assert await history(env, anna) == []


# --------------------------------------------------------------------------- три состояния повтора


async def test_repeat_when_reply_saved_replays_without_model_or_limit(
    env: Env, engine: AsyncEngine
) -> None:
    """(а) Ответ сохранён: повтор — `accepted` (`replayed`), сохранённый текст одним `delta` и
    `done` с тем же id; модель не вызывается, лимит не расходуется, новой реплики нет."""
    p = stub()
    env.use(p)
    env.box.limit = 1
    anna = await _person(env.client)
    key = uuid.uuid4()
    _, first = await send(env, anna, "Привет", key)
    calls = (len(p.requests), len(p.stream_requests))
    for _ in range(2):
        resp, again = await send(env, anna, "Привет", key)
        assert resp.status_code == 200
        assert names(again) == ["accepted", "delta", "done"]
        assert again[0][1] == {**first[0][1], "replayed": True}
        assert joined(again) == joined(first) == ANSWER["reply"]
        assert again[-1][1] == {"message_id": first[-1][1]["message_id"], "suggest_draft": False}
    assert (len(p.requests), len(p.stream_requests)) == calls
    assert len(await history(env, anna)) == 2
    # Лимит 1 израсходован одной репликой: новая (другой ключ) — 429.
    resp, _ = await send(env, anna, "Другое", uuid.uuid4())
    _error(resp, 429, "rate_limited")


async def test_repeat_when_saved_without_reply_answers_that_turn(
    env: Env, engine: AsyncEngine
) -> None:
    """(б) Реплика сохранена, ответа нет (ошибка модели): повтор с тем же ключом новой реплики
    не создаёт, генерирует ответ на неё; лимит за неё уже засчитан — повтор не расходует его и не
    получает 429 (АР-32); `accepted.message_id` — тот же."""
    env.use(stub(chunks=[LLMUnavailable("down")]))
    env.box.limit = 1
    anna = await _person(env.client)
    key, body = uuid.uuid4(), mark("x")
    _, first = await send(env, anna, body, key)
    assert names(first) == ["accepted", "error"] and first[0][1]["replayed"] is False
    p = stub()
    env.use(p)
    resp, again = await send(env, anna, body, key)
    assert resp.status_code == 200 and names(again)[-1] == "done"
    assert again[0][1] == {**first[0][1], "replayed": True}
    agent, owner = await history(env, anna)
    assert owner["id"] == first[0][1]["message_id"] and agent["reply_to"] == owner["id"]
    assert body in p.stream_requests[0]["messages"][-1][1]
    # Ответ сохранён — следующий повтор отдаёт его.
    _, third = await send(env, anna, body, key)
    assert names(third) == ["accepted", "delta", "done"]
    assert third[-1][1]["message_id"] == agent["id"]


async def test_repeat_after_429_is_charged_once_when_accepted(env: Env) -> None:
    """429 реплику с ключом не засчитывает; повтор после нового окна — ответ на ту же реплику и
    засчитан один раз."""
    env.box.limit = 1
    anna = await _person(env.client)
    _, ok = await send(env, anna, "первая", uuid.uuid4())
    assert names(ok)[-1] == "done"
    key = uuid.uuid4()
    resp, _ = await send(env, anna, "вторая", key)
    _error(resp, 429, "rate_limited")
    resp, _ = await send(env, anna, "вторая", key)
    _error(resp, 429, "rate_limited")
    env.box.limit = 2
    _, again = await send(env, anna, "вторая", key)
    assert names(again)[-1] == "done" and again[0][1]["replayed"] is True
    assert [i["author"] for i in await history(env, anna)] == ["agent", "owner", "agent", "owner"]
    resp, _ = await send(env, anna, "третья", uuid.uuid4())
    _error(resp, 429, "rate_limited")


async def test_repeat_while_reply_in_progress_is_409(env: Env, engine: AsyncEngine) -> None:
    """(в) Ответ готовит другой запрос (аренда не истекла) — 409 `conflict_retry` до потока, без
    вызова модели и без новой реплики. Истёкшая аренда (сбой процесса) — повтор отвечает."""
    p = stub()
    env.use(p)
    anna = await _person(env.client)
    key = uuid.uuid4()
    env.use(stub(chunks=[LLMUnavailable("down")]))
    await send(env, anna, "Привет", key)
    env.use(p)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE conversation_messages SET reply_lease_until = now() + interval '1 minute' "
                "WHERE client_message_id = :k"
            ),
            {"k": key},
        )
    resp, _ = await send(env, anna, "Привет", key)
    _error(resp, 409, "conflict_retry")
    assert resp.headers["cache-control"] == "no-store"
    assert p.requests == [] and p.stream_requests == []
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE conversation_messages SET reply_lease_until = now() - interval '1 second' "
                "WHERE client_message_id = :k"
            ),
            {"k": key},
        )
    _, events = await send(env, anna, "Привет", key)
    assert names(events)[-1] == "done" and events[0][1]["replayed"] is True
    assert [i["author"] for i in await history(env, anna)] == ["agent", "owner"]


async def test_same_key_other_text_is_422_and_nothing_saved(env: Env) -> None:
    env.use(stub())
    anna = await _person(env.client)
    key = uuid.uuid4()
    await send(env, anna, "Привет", key)
    resp, _ = await send(env, anna, "Совсем другое", key)
    _error(resp, 422, "validation_error")
    assert [i["text"] for i in await history(env, anna)] == [ANSWER["reply"], "Привет"]


async def test_repeat_after_later_turns_answers_with_context_up_to_that_turn(
    env: Env,
) -> None:
    """MAJOR-1: X без ответа, затем Y с ответом; повтор X — ответ на X (контекст — до X, Y в него
    не входит), привязан к X через `reply_to`; дубля X нет."""
    env.use(stub(chunks=[LLMUnavailable("down")]))
    anna = await _person(env.client)
    kx, x, y = uuid.uuid4(), mark("x"), mark("y")
    _, first = await send(env, anna, x, kx)
    env.use(stub())
    await send(env, anna, y, uuid.uuid4())
    p = stub()
    env.use(p)
    _, again = await send(env, anna, x, kx)
    assert names(again)[-1] == "done"
    dialog = p.stream_requests[0]["messages"][-1][1]
    assert x in dialog and y not in dialog
    items = await history(env, anna)
    assert [i["author"] for i in items] == ["agent", "agent", "owner", "owner"]
    assert items[0]["reply_to"] == first[0][1]["message_id"] == items[3]["id"]
    assert items[1]["reply_to"] == items[2]["id"]
    assert sum(i["text"] == x for i in items) == 1


# --------------------------------------------------------------------------- изоляция и гонка


async def test_same_key_of_different_users_are_independent(env: Env, engine: AsyncEngine) -> None:
    """Ключ уникален в пределах владельца: тот же ключ у другого человека — его новая реплика;
    чужой ответ не отдаётся и не раскрывается."""
    env.use(stub())
    anna, boris = await _person(env.client), await _person(env.client, "Борис", "Вера")
    key = uuid.uuid4()
    secret = mark("anna")
    _, a = await send(env, anna, secret, key)
    _, b = await send(env, boris, "Привет", key)
    assert a[0][1]["replayed"] is False and b[0][1]["replayed"] is False
    assert a[0][1]["message_id"] != b[0][1]["message_id"]
    assert all(secret not in i["text"] for i in await history(env, boris))
    assert len(await history(env, anna)) == len(await history(env, boris)) == 2
    # Другой текст под тем же ключом у Бориса — не 422 из-за Анны: его ключ уже его.
    resp, _ = await send(env, boris, secret, key)
    _error(resp, 422, "validation_error")


async def test_two_concurrent_requests_with_same_key_make_one_turn(
    env: Env, engine: AsyncEngine
) -> None:
    """Гонка: два одновременных запроса с одним ключом — одна реплика и один ответ; второй
    получает 409 (первый ещё отвечает); модель вызвана один раз; лимит засчитан один раз."""
    p = stub(chunk_delay_s=0.05)
    env.use(p)
    anna = await _person(env.client)
    key = uuid.uuid4()
    (r1, e1), (r2, e2) = await asyncio.gather(
        send(env, anna, "Привет", key), send(env, anna, "Привет", key)
    )
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 409], statuses
    events = e1 or e2
    assert names(events)[-1] == "done"
    assert len(p.stream_requests) == 1
    conflict = r1 if r1.status_code == 409 else r2
    _error(conflict, 409, "conflict_retry")
    db = await rows(engine, anna.id)
    assert [r["author"] for r in db] == ["owner", "agent"]
    _, replay = await send(env, anna, "Привет", key)
    assert names(replay) == ["accepted", "delta", "done"]


async def test_db_keeps_one_turn_per_key_and_reply_in_same_conversation(
    env: Env, engine: AsyncEngine
) -> None:
    """Схема 0018: ключ уникален в диалоге; ответ — не больше одного на реплику и только в том
    же диалоге (составной внешний ключ); ключ и аренда — только у реплик владельца, связь и флаг
    кризиса — только у ответов агента."""
    env.use(stub())
    anna, boris = await _person(env.client), await _person(env.client, "Борис", "Вера")
    key = uuid.uuid4()
    await send(env, anna, "Привет", key)
    await send(env, boris, "Привет", uuid.uuid4())
    a_rows, b_rows = await rows(engine, anna.id), await rows(engine, boris.id)
    a_turn, a_reply, b_turn = a_rows[0]["id"], a_rows[1]["id"], b_rows[0]["id"]

    async def fails(sql: str, params: dict[str, Any], code: str) -> None:
        async with engine.connect() as conn:
            with pytest.raises(DBAPIError) as err:
                await conn.execute(text(sql), params)
            await conn.rollback()
        assert getattr(err.value.orig, "sqlstate", None) == code, err.value

    copy_turn = (
        "INSERT INTO conversation_messages (conversation_id, author, text_enc, {col}) "
        "SELECT conversation_id, CAST(:au AS message_author), text_enc, :v "
        "FROM conversation_messages WHERE id = :src"
    )
    await fails(
        copy_turn.format(col="client_message_id"), {"au": "owner", "v": key, "src": a_turn}, "23505"
    )
    await fails(
        copy_turn.format(col="reply_to_id"), {"au": "agent", "v": a_turn, "src": a_reply}, "23505"
    )
    # Ответ Бориса не привязать к реплике Анны (её ответ убран, чтобы проверить именно ключ
    # диалога, а не «один ответ на реплику»): другой диалог.
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM conversation_messages WHERE id = :i"), {"i": a_reply})
    await fails(
        "UPDATE conversation_messages SET reply_to_id = :t WHERE id = :i",
        {"t": a_turn, "i": b_rows[1]["id"]},
        "23503",
    )
    await fails(
        "UPDATE conversation_messages SET crisis = true WHERE id = :i", {"i": b_turn}, "23514"
    )
    await fails(
        "UPDATE conversation_messages SET client_message_id = :k WHERE id = :i",
        {"k": uuid.uuid4(), "i": b_rows[1]["id"]},
        "23514",
    )
    await fails(
        "UPDATE conversation_messages SET limit_charged = true WHERE id = :i",
        {"i": b_rows[1]["id"]},
        "23514",
    )


async def test_partner_cannot_read_or_touch_key_columns(env: Env, engine: AsyncEngine) -> None:
    """RLS 0001 держит и новые колонки: под ролью приложения другой человек не видит и не
    меняет чужую реплику (ни ключ, ни аренду, ни отметку лимита)."""
    env.use(stub())
    anna, boris = await _person(env.client), await _person(env.client, "Борис", "Вера")
    key = uuid.uuid4()
    await send(env, anna, "Привет", key)
    sm = env.sm
    async with user_scope(sm, boris.id) as s:
        found = (
            await s.execute(
                text("SELECT count(*) FROM conversation_messages WHERE client_message_id = :k"),
                {"k": key},
            )
        ).scalar_one()
        changed = (
            await s.execute(
                text(
                    "UPDATE conversation_messages SET reply_lease_until = NULL, "
                    "limit_charged = false WHERE client_message_id = :k RETURNING id"
                ),
                {"k": key},
            )
        ).all()
    assert found == 0 and changed == []
    turn = (await rows(engine, anna.id))[0]["id"]
    async with engine.begin() as conn:
        lease = (
            await conn.execute(
                text(
                    "UPDATE conversation_messages SET reply_lease_until = now() + interval '1 min' "
                    "WHERE id = :i RETURNING reply_lease_until"
                ),
                {"i": turn},
            )
        ).scalar_one()
    assert not await chat_repo.release_reply_lease(sm, boris.id, turn, lease)
    assert not await chat_repo.mark_limit_charged(sm, boris.id, turn)
    assert await chat_repo.release_reply_lease(sm, anna.id, turn, lease)


# --------------------------------------------------------------------------- кризис


async def test_crisis_flag_in_history_and_replay_with_contacts(
    env: Env, engine: AsyncEngine
) -> None:
    """MINOR-3 / предложение 3: кризисный ответ в истории — `crisis: true` с `reply_to`; повтор с
    тем же ключом — `crisis` с тем же текстом и контактами, без классификатора и без второго
    события безопасности; лимит кризис не блокирует (§9.3)."""
    env.box.limit = 1
    anna = await _person(env.client)
    await send(env, anna, "обычная", uuid.uuid4())  # лимит исчерпан
    p = stub(category="self_harm")
    env.use(p)
    key = uuid.uuid4()
    _, first = await send(env, anna, mark("crisis"), key)
    assert names(first) == ["accepted", "crisis"]
    crisis = first[-1][1]
    agent, owner = (await history(env, anna))[:2]
    assert agent["crisis"] is True and agent["reply_to"] == owner["id"] == first[0][1]["message_id"]
    assert owner["crisis"] is False and agent["id"] == crisis["message_id"]
    calls = len(p.requests)
    await env.rt.drain()
    _, again = await send(env, anna, owner["text"], key)
    assert names(again) == ["accepted", "crisis"]
    assert again[-1][1] == {**crisis, "replace": False}
    assert again[-1][1]["contacts"] == (await env.client.get("/api/v1/help/contacts")).json()
    assert len(p.requests) == calls
    await env.rt.drain()
    async with engine.connect() as conn:
        n = (
            await conn.execute(
                text("SELECT count(*) FROM safety_events WHERE user_id = :u"), {"u": anna.id}
            )
        ).scalar_one()
    assert n == 1


async def test_unsaved_turn_with_key_crisis_has_null_message_id(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Реплику сохранить не удалось (не 409): с ключом — `accepted` с `message_id: null`, затем
    `crisis` с контактами (§9.3); повтор с тем же ключом сохраняет её заново."""
    env.use(stub(category="self_harm"))
    anna = await _person(env.client)

    async def broken(*args: Any, **kw: Any) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(chat_repo, "accept_owner_turn", broken)
    key = uuid.uuid4()
    _, events = await send(env, anna, "Мне очень плохо", key)
    assert names(events) == ["accepted", "crisis"]
    assert events[0][1] == {"message_id": None, "client_message_id": str(key), "replayed": False}
    monkeypatch.undo()
    _, again = await send(env, anna, "Мне очень плохо", key)
    assert again[0][1]["message_id"] is not None and again[0][1]["replayed"] is False


async def test_unique_violation_on_insert_is_409(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Страховка: 23505 при вставке ключа (не должна случаться при блокировке агента) — 409, не
    500; повтор проходит."""
    env.use(stub())
    anna = await _person(env.client)
    real = chat_repo._insert_row

    async def dup(s: AsyncSession, *args: Any, **kw: Any) -> Any:
        await real(s, *args, **kw)
        await s.execute(text("DO $$ BEGIN RAISE EXCEPTION 'dup' USING ERRCODE = '23505'; END $$"))

    monkeypatch.setattr(chat_repo, "_insert_row", dup)
    key = uuid.uuid4()
    resp, _ = await send(env, anna, "Привет", key)
    _error(resp, 409, "conflict_retry")
    monkeypatch.undo()
    assert await history(env, anna) == []
    _, events = await send(env, anna, "Привет", key)
    assert names(events)[-1] == "done" and events[0][1]["replayed"] is False


# --------------------------------------------------------------------------- обрыв и журналы


async def test_client_gone_mid_stream_releases_lease_then_repeat_answers(
    env: Env, engine: AsyncEngine
) -> None:
    """Клиент ушёл посреди ответа: недописанный ответ не сохранён, аренда снята (фоновой задачей,
    отмена её не отменяет) — повтор с тем же ключом сразу отвечает на ту же реплику, без 409."""
    p = stub(chunks=json_chunks(ANSWER, 60), chunk_delay_s=30.0)
    env.use(p)
    anna = await _person(env.client)
    key = uuid.uuid4()
    gone = asyncio.Event()
    sent = False
    body = json.dumps({"text": "Привет", "client_message_id": str(key)}).encode()
    seen: list[bytes] = []

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send_(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            seen.append(message["body"])
            if b"event: delta" in message["body"]:
                gone.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": API,
        "raw_path": API.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"test"),
            (b"authorization", f"Bearer {anna.token}".encode()),
            (b"content-type", b"application/json"),
        ],
        "server": ("test", 80),
        "client": ("127.0.0.1", 12345),
    }
    await asyncio.wait_for(app(scope, receive, send_), 5)
    raw = b"".join(seen).decode()
    assert raw.startswith("event: accepted") and "event: done" not in raw
    await env.rt.drain()
    db = await rows(engine, anna.id)
    assert [r["author"] for r in db] == ["owner"] and db[0]["reply_lease_until"] is None
    env.use(stub())
    _, events = await send(env, anna, "Привет", key)
    assert names(events)[-1] == "done" and events[0][1]["replayed"] is True


async def test_logs_have_no_texts_or_keys(
    env: Env, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """В журналах — ни текста реплики и ответа, ни ключа, ни id (повтор, 409, 422 тоже)."""
    caplog.set_level(logging.DEBUG)
    reply = mark("reply")
    env.use(stub({**ANSWER, "reply": reply}))
    anna = await _person(env.client)
    key, body = uuid.uuid4(), mark("owner")
    _, events = await send(env, anna, body, key)
    await send(env, anna, body, key)
    await send(env, anna, mark("other"), key)
    await env.rt.drain()
    logged = "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)
    assert "event=replayed" in logged
    for secret in (reply, body, str(key), key.hex, events[0][1]["message_id"]):
        assert secret not in logged
    async with engine.connect() as conn:
        for table in ("audit_log", "safety_events"):
            dump = (
                await conn.execute(
                    text(f"SELECT coalesce(string_agg(t::text, ' '), '') FROM {table} t")
                )
            ).scalar_one()
            assert body not in dump and reply not in dump and str(key) not in dump


# --------------------------------------------------------------------------- обратимость 0018

NEW_COLUMNS = {"client_message_id", "reply_to_id", "crisis", "reply_lease_until", "limit_charged"}


async def _columns(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as c:
        res = await c.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'conversation_messages'"
            )
        )
        return {r[0] for r in res}


async def test_migration_0018_reversible(
    env: Env, engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """head → 0017 → head: колонки, индексы и ограничения уходят и возвращаются; реплики и их
    шифротексты сохраняются; после возврата — ключи пусты (данные 0018 не восстанавливаются), а
    чат с ключом снова работает; ``alembic check`` чист."""
    env.use(stub())
    anna = await _person(env.client)
    await send(env, anna, "Привет", uuid.uuid4())
    await env.rt.drain()
    dump_sql = (
        "SELECT md5(string_agg(m.id::text || m.author::text || encode(m.text_enc, 'hex'), '|' "
        "ORDER BY m.id)) FROM conversation_messages m JOIN conversations c "
        "ON c.id = m.conversation_id JOIN agents a ON a.id = c.agent_id WHERE a.user_id = :u"
    )
    async with engine.connect() as c:
        before = (await c.execute(text(dump_sql), {"u": anna.id})).scalar_one()
    try:
        alembic("downgrade", "0017")
        await engine.dispose()
        assert NEW_COLUMNS.isdisjoint(await _columns(engine))
        async with engine.connect() as c:
            assert (await c.execute(text(dump_sql), {"u": anna.id})).scalar_one() == before
            left = (
                await c.execute(
                    text(
                        "SELECT count(*) FROM pg_constraint WHERE conrelid = "
                        "'conversation_messages'::regclass AND conname IN ("
                        "'uq_conversation_messages_conversation_id_id', "
                        "'fk_conversation_messages_reply_to_id_conversation_messages', "
                        "'ck_conversation_messages_owner_only_fields', "
                        "'ck_conversation_messages_agent_only_fields')"
                    )
                )
            ).scalar_one()
            assert left == 0
        alembic("upgrade", "head")
        await engine.dispose()
        assert NEW_COLUMNS <= await _columns(engine)
        alembic("check")
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    async with engine.connect() as c:
        assert (await c.execute(text(dump_sql), {"u": anna.id})).scalar_one() == before
    db = await rows(engine, anna.id)
    assert all(r["client_message_id"] is None and r["reply_to_id"] is None for r in db)
    key = uuid.uuid4()
    _, events = await send(env, anna, "Снова", key)
    assert names(events)[-1] == "done"
    _, again = await send(env, anna, "Снова", key)
    assert names(again) == ["accepted", "delta", "done"]

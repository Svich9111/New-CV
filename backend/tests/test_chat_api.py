"""2.1, этап 2 (A2): личный чат по HTTP — `POST /chat/messages` (SSE), `GET /chat/messages`,
`GET /help/contacts`, дневной лимит.

Критерии — docs/tasks/2.1.md («Правила для этапа 2», критерии готовности); ФТ-05, ФТ-17;
ТЗ §6, §9.3, §10, §11 п.5; DECISIONS.md АР-20, АР-24, АР-30, АР-32, АР-33. Модель — заглушка
`app.ai.stub.ScriptedStreamProvider` (сеть к OpenRouter не нужна), БД — тестовый PostgreSQL с
миграциями, приложение — под ролью входа `feelings_api`. Данные выдуманные; тексты помечены
уникальными метками — утечки ищутся поиском меток."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import socket
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.ai.context import ContextBudget
from app.ai.llm import (
    LLMAuthError,
    LLMBadOutput,
    LLMConfigError,
    LLMQuotaError,
    LLMRefusal,
    LLMUnavailable,
    StructuredResult,
)
from app.ai.models import AgentModels
from app.ai.stub import ScriptedProvider, ScriptedStreamProvider, json_chunks
from app.ai.summary import CompressionPolicy
from app.api import chat as chat_api
from app.api.chat import ChatRuntime
from app.config import ConfigError, Settings, get_settings
from app.core.crypto import Keyring, generate_key_entry, parse_keyring
from app.core.limits import MemoryDailyLimit, RedisDailyLimit, window
from app.core.session import issue_token
from app.help import HELP_CONTACTS, check_help_contacts_config, help_contacts
from app.main import app
from app.repositories import chat as chat_repo
from tests.contract import assert_matches
from tests.test_auth_api import SECRET, TOKEN, _init_data, _tg
from tests.test_couple_api import BOT, _create

SM = async_sessionmaker[AsyncSession]
API = "/api/v1/chat/messages"

ANSWER: dict[str, Any] = {
    "level": 2,
    "reply": "Слышу, как тебе обидно. Что задело сильнее всего — сама отмена или то, как поздно?",
    "draft_ready": False,
    "crisis_type": None,
    "crisis_secondary": [],
}
EVENT_SCHEMAS = {
    "delta": "ChatStreamDelta",
    "done": "ChatStreamDone",
    "crisis": "ChatStreamCrisis",
    "error": "ChatStreamError",
}


# --------------------------------------------------------------------------- заглушки модели


def handler(
    category: str = "none", summary: Callable[[dict[str, Any]], dict[str, Any]] | None = None
) -> Any:
    def h(role: str, payload: dict[str, Any], system: str) -> dict[str, Any]:
        if role == "safety":
            return {"category": category, "secondary": []}
        if role == "summary":
            if summary is None:
                return {"summary": "Владелец рассказывал о планах на выходные."}
            return summary(payload)
        raise AssertionError(role)

    return h


@dataclass
class Tracking(ScriptedStreamProvider):
    """Поток агента с учётом: сколько вызовов начато, отменено и закрыто."""

    started: int = 0
    cancelled: int = 0
    closed: int = 0

    async def stream_text(self, **kw: Any) -> AsyncIterator[str]:  # type: ignore[override]
        self.started += 1
        try:
            async for chunk in super().stream_text(**kw):
                yield chunk
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.closed += 1


def stub(
    answer: Mapping[str, Any] | None = None,
    *,
    category: str = "none",
    chunks: Sequence[Any] | None = None,
    first_delay_s: float = 0.0,
    chunk_delay_s: float = 0.0,
    summary: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> Tracking:
    items = list(chunks) if chunks is not None else json_chunks(answer or ANSWER, 9)
    return Tracking(
        handler(category, summary),
        stream=lambda payload: items,
        first_delay_s=first_delay_s,
        chunk_delay_s=chunk_delay_s,
    )


class SlowSafety:
    """Классификатор, который отвечает долго (для отключения клиента на шаге классификатора)."""

    model = "slow-safety"

    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.started = 0
        self.cancelled = 0

    async def complete_json(self, **kw: Any) -> StructuredResult:
        self.started += 1
        try:
            await asyncio.sleep(self.delay_s)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return await ScriptedProvider(handler()).complete_json(**kw)

    def stream_text(self, **kw: Any) -> Any:  # pragma: no cover — не вызывается
        raise NotImplementedError


def models(dialog_p: Any, safety_p: Any | None = None) -> AgentModels:
    s = safety_p if safety_p is not None else dialog_p
    return AgentModels(dialog_p, dialog_p, dialog_p, s, s, s)


# --------------------------------------------------------------------------- окружение


@dataclass
class Box:
    limit: int = 100
    keepalive_s: float = 15.0


@dataclass
class Env:
    client: httpx.AsyncClient
    rt: ChatRuntime
    box: Box
    keys: Keyring
    sm: SM

    def use(self, provider: Any, safety_p: Any | None = None) -> None:
        self.rt.models = models(provider, safety_p)


@pytest.fixture
async def env(sessionmaker: SM) -> AsyncIterator[Env]:
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


@dataclass
class U:
    headers: dict[str, str]
    id: uuid.UUID
    token: str = ""


async def _login(c: httpx.AsyncClient) -> U:
    resp = await c.post("/api/v1/auth/telegram", json={"init_data": _init_data(_tg())})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    token = body["access_token"]
    return U({"Authorization": f"Bearer {token}"}, uuid.UUID(body["me"]["id"]), token)


async def _person(c: httpx.AsyncClient, name: str = "Анна", partner: str = "Игорь") -> U:
    user = await _login(c)
    resp = await c.put(
        "/api/v1/me/onboarding",
        headers=user.headers,
        json={
            "name": name,
            "address_form": "на ты",
            "partner_name": partner,
            "timezone": "Europe/Moscow",
            "consent_terms": True,
            "consent_data": True,
            "consent_sensitive": True,
            "age_confirmed": True,
        },
    )
    assert resp.status_code == 200, resp.text
    return user


def mark(what: str) -> str:
    return f"метка-{what}-{uuid.uuid4().hex}"


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Разбор потока строго по контракту: `event:` + `data:` (JSON в одну строку) + пустая
    строка; строки `:` — поддержание соединения; `id:`/`retry:` нет. Каждое событие — по своей
    схеме; `delta`*, затем ровно одно завершающее событие в конце."""
    assert body.endswith("\n\n"), body[-40:]
    events: list[tuple[str, dict[str, Any]]] = []
    for block in body.split("\n\n")[:-1]:
        lines = block.split("\n")
        if all(line.startswith(":") for line in lines):
            continue
        assert len(lines) == 2, block
        assert lines[0].startswith("event: ") and lines[1].startswith("data: "), block
        name, data = lines[0][len("event: ") :], json.loads(lines[1][len("data: ") :])
        assert_matches(data, EVENT_SCHEMAS[name])
        events.append((name, data))
    names = [n for n, _ in events]
    assert names and names[-1] in ("done", "crisis", "error"), names
    assert all(n == "delta" for n in names[:-1]), names
    return events


async def post(env: Env, user: U, body: str) -> tuple[httpx.Response, list[tuple[str, Any]]]:
    resp = await env.client.post(API, headers=user.headers, json={"text": body})
    if resp.status_code != 200:
        return resp, []
    assert resp.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-accel-buffering"] == "no"
    return resp, parse_sse(resp.text)


def joined(events: Sequence[tuple[str, Any]]) -> str:
    return "".join(d["text"] for n, d in events if n == "delta")


async def history(env: Env, user: U, **params: Any) -> list[dict[str, Any]]:
    resp = await env.client.get(API, headers=user.headers, params=params or None)
    assert resp.status_code == 200, resp.text
    items: list[dict[str, Any]] = resp.json()["items"]
    return items


def _error(resp: httpx.Response, status: int, code: str) -> None:
    assert resp.status_code == status, resp.text
    assert_matches(resp.json(), "Error")
    assert resp.json()["code"] == code, resp.json()


# --------------------------------------------------------------------------- ASGI-клиент


@dataclass
class Raw:
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    chunks: list[tuple[float, bytes]] = field(default_factory=list)

    @property
    def body(self) -> str:
        return b"".join(c for _, c in self.chunks).decode()


async def raw_post(
    user: U,
    body: str,
    *,
    disconnect: Callable[[Raw], bool] | None = None,
    disconnect_after_s: float | None = None,
    timeout_s: float = 5.0,
) -> Raw:
    """Прямой вызов ASGI-приложения: поток виден по кускам (со временем), клиент может уйти
    (``http.disconnect``) по условию или по таймеру."""
    raw = Raw()
    gone = asyncio.Event()
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            payload = json.dumps({"text": body}).encode()
            return {"type": "http.request", "body": payload, "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            raw.status = message["status"]
            raw.headers = {k.decode(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body" and message.get("body"):
            raw.chunks.append((time.monotonic(), message["body"]))
        if disconnect is not None and disconnect(raw):
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
            (b"authorization", f"Bearer {user.token}".encode()),
            (b"content-type", b"application/json"),
        ],
        "server": ("test", 80),
        "client": ("127.0.0.1", 12345),
    }
    timer = None
    if disconnect_after_s is not None:
        timer = asyncio.get_running_loop().call_later(disconnect_after_s, gone.set)
    try:
        await asyncio.wait_for(app(scope, receive, send), timeout_s)
    finally:
        if timer is not None:
            timer.cancel()
    return raw


# --------------------------------------------------------------------------- поток: обычный ответ


async def test_stream_deltas_then_done_and_reply_is_saved(env: Env) -> None:
    """SSE по контракту: несколько `delta`, затем `done`; склейка `delta` — ровно сохранённая
    реплика агента (`done.message_id`); реплика владельца сохранена раньше ответа."""
    p = stub()
    env.use(p)
    anna = await _person(env.client)
    owner_text = mark("owner")
    resp, events = await post(env, anna, owner_text)
    assert resp.status_code == 200
    assert sum(n == "delta" for n, _ in events) > 1
    name, done = events[-1]
    assert name == "done" and done["suggest_draft"] is False
    assert joined(events) == ANSWER["reply"]
    items = await history(env, anna)
    assert [(i["author"], i["text"]) for i in items] == [
        ("agent", joined(events)),
        ("owner", owner_text),
    ]
    assert items[0]["id"] == done["message_id"]
    for item in items:
        assert_matches(item, "ChatMessage")
    # Вход агента — из сборки контекста §6.1 (реплика владельца в диалоге).
    assert owner_text in p.stream_requests[0]["messages"][-1][1]


async def test_joined_deltas_equal_saved_text_even_with_outer_spaces(env: Env) -> None:
    """`Done.reply` у A3 — без пробелов по краям, а куски — как пришли; API сохраняет склейку
    кусков, поэтому равенство «склейка = сохранённое» — точное (контракт)."""
    env.use(stub({**ANSWER, "reply": "  Понимаю тебя.\nРасскажи ещё.  ", "draft_ready": True}))
    anna = await _person(env.client)
    _, events = await post(env, anna, "Привет")
    assert events[-1][0] == "done" and events[-1][1]["suggest_draft"] is True
    saved = (await history(env, anna))[0]
    assert saved["text"] == joined(events) == "  Понимаю тебя.\nРасскажи ещё.  "


async def test_first_delta_does_not_wait_for_full_answer(env: Env) -> None:
    """Первый кусок уходит клиенту до конца ответа модели (ТЗ §10; заглушка с задержкой)."""
    env.use(stub(chunk_delay_s=0.05))
    anna = await _person(env.client)
    raw = await raw_post(anna, "Привет")
    assert raw.status == 200
    events = parse_sse(raw.body)
    assert events[-1][0] == "done"
    delta_times = [t for t, c in raw.chunks if c.startswith(b"event: delta")]
    end_time = raw.chunks[-1][0]
    assert len(delta_times) > 3
    assert end_time - delta_times[0] > 0.2  # первый кусок — задолго до конца


async def test_keepalive_comments_while_waiting(env: Env) -> None:
    """Пока событий нет дольше `FEELINGS_CHAT_KEEPALIVE_S` — строки-комментарии `:`."""
    env.box.keepalive_s = 0.05
    env.use(stub(first_delay_s=0.4))
    anna = await _person(env.client)
    resp, events = await post(env, anna, "Привет")
    assert ": keep-alive\n\n" in resp.text
    assert events[-1][0] == "done"


# --------------------------------------------------------------------------- классификатор


async def test_classifier_gets_built_context_whole_not_truncated_dialog(env: Env) -> None:
    """Правило этапа 2: `screen(models, built)` — классификатор видит три последние реплики
    владельца, даже если бюджет выбросил их из диалога агента."""
    p = stub()
    env.use(p)
    # Бюджет: системный промпт + последняя реплика — старые реплики в диалог агента не влезают.
    from app.ai.prompts import system_prompt

    env.rt.budget = ContextBudget(max_chars=len(system_prompt("dialog").text) + 2_200)
    anna = await _person(env.client)
    marks = [mark(f"t{i}") for i in range(3)]
    for m in marks:
        await post(env, anna, m + " " + "длинная реплика " * 120)
    agent_input = p.stream_requests[-1]["messages"][-1][1]
    assert marks[2] in agent_input and marks[0] not in agent_input  # диалог агента усечён
    safety_input = [r for r in p.requests if r["role"] == "safety"][-1]["messages"][-1][1]
    assert all(m in safety_input for m in marks)  # классификатору — все три


# --------------------------------------------------------------------------- кризис


async def test_crisis_from_classifier_before_agent(env: Env, engine: AsyncEngine) -> None:
    """Кризис второй линии: событие `crisis` до любого `delta`, агент не вызывается, контакты —
    те же, что `GET /help/contacts`; ответ сохранён; в `safety_events` — только тип."""
    p = stub(category="self_harm")
    env.use(p)
    anna = await _person(env.client)
    _, events = await post(env, anna, mark("crisis"))
    assert [n for n, _ in events] == ["crisis"]
    crisis = events[0][1]
    assert crisis["replace"] is False
    assert p.started == 0 and p.stream_requests == []
    contacts = (await env.client.get("/api/v1/help/contacts", headers=anna.headers)).json()
    assert crisis["contacts"] == contacts
    items = await history(env, anna)
    assert items[0]["author"] == "agent" and items[0]["text"] == crisis["text"]
    assert items[0]["id"] == crisis["message_id"]
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT * FROM safety_events WHERE user_id = :u"), {"u": anna.id}
            )
        ).mappings()
        rows_l = [dict(r) for r in rows]
    assert [r["type"] for r in rows_l] == ["self_harm"]
    assert set(rows_l[0]) <= {"id", "user_id", "type", "created_at"}


async def test_crisis_from_agent(env: Env, engine: AsyncEngine) -> None:
    """Кризис распознал сам агент: его ответ и блоки протокола; `replace` — по
    `Crisis.replaces_streamed`; тип — в `safety_events`."""
    env.use(stub({**ANSWER, "reply": "Мне очень тревожно за тебя.", "crisis_type": "violence"}))
    anna = await _person(env.client)
    _, events = await post(env, anna, "Он меня ударил")
    name, crisis = events[-1]
    assert name == "crisis"
    shown = joined(events).strip()
    assert shown == "Мне очень тревожно за тебя."
    assert crisis["replace"] is (not crisis["text"].startswith(shown))
    assert crisis["contacts"] == help_contacts()
    assert (await history(env, anna))[0]["text"] == crisis["text"]
    async with engine.connect() as conn:
        types = (
            await conn.execute(
                text("SELECT type::text FROM safety_events WHERE user_id = :u"), {"u": anna.id}
            )
        ).scalars()
        assert list(types) == ["violence"]


async def test_crisis_event_even_if_reply_not_saved(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой записи ответа не отменяет кризисный ответ: `message_id` null, контакты есть."""
    env.use(stub(category="life_threat"))
    anna = await _person(env.client)
    real = chat_repo.add_message

    async def broken(*args: Any, **kw: Any) -> Any:
        if args[4] == "agent":
            raise RuntimeError("db down")
        return await real(*args, **kw)

    monkeypatch.setattr(chat_repo, "add_message", broken)
    _, events = await post(env, anna, "Не хочу жить")
    assert events[-1][0] == "crisis"
    assert events[-1][1]["message_id"] is None and events[-1][1]["contacts"]


# --------------------------------------------------------------------------- ошибки


@pytest.mark.parametrize(
    ("chunks", "code"),
    [
        ([LLMRefusal("refusal")], "refusal"),
        ([LLMUnavailable("down")], "unavailable"),
        ([LLMQuotaError("quota")], "quota"),
        (['{"level": 2, "reply": "Слышу', LLMBadOutput("cut")], "bad_output"),
        (["это не JSON"], "bad_output"),
        ([LLMConfigError("no key")], "internal"),
        ([LLMAuthError("bad key")], "internal"),
        ([RuntimeError("bug")], "internal"),
    ],
)
async def test_error_codes_and_owner_message_kept(env: Env, chunks: list[Any], code: str) -> None:
    """Каждый код `ChatErrorCode`; реплика владельца сохранена, ответа агента нет, текста
    реплики в событии нет."""
    env.use(stub(chunks=chunks))
    anna = await _person(env.client)
    owner_text = mark("err")
    _, events = await post(env, anna, owner_text)
    name, err = events[-1]
    assert (name, err["code"]) == ("error", code)
    assert err["message"] and owner_text not in err["message"]
    assert [(i["author"], i["text"]) for i in await history(env, anna)] == [("owner", owner_text)]


async def test_models_not_configured_is_internal_error(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Модели — из конфигурации (OpenRouter): без ключа — `error` `internal`, реплика сохранена;
    сеть не нужна (ключ проверяется до запроса)."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    env.rt.models = None
    anna = await _person(env.client)
    _, events = await post(env, anna, "Привет")
    name, err = events[-1]
    assert (len(events), name, err["code"]) == (1, "error", "internal")
    # Ни одна линия не дала вердикта — в сообщении указание на помощь (АР-34 п.2).
    assert err["message"].startswith(chat_api.INTERNAL_MESSAGE) and "112" in err["message"]
    assert env.rt.models is None  # ошибка не кешируется
    assert [i["author"] for i in await history(env, anna)] == ["owner"]


async def test_reply_save_failure_is_internal_error(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ответ агента не сохранился (владелец удалён во время ответа, Б-44) — `error` `internal`,
    а не `done` с несуществующим id."""
    anna = await _person(env.client)
    real = chat_repo.add_message

    async def gone(*args: Any, **kw: Any) -> Any:
        return None if args[4] == "agent" else await real(*args, **kw)

    monkeypatch.setattr(chat_repo, "add_message", gone)
    _, events = await post(env, anna, "Привет")
    assert events[-1] == ("error", {"code": "internal", "message": chat_api.INTERNAL_MESSAGE})


# --------------------------------------------------------------------------- отключение клиента


def _no_new_tasks(before: set[asyncio.Task[Any]]) -> list[asyncio.Task[Any]]:
    current = asyncio.current_task()
    return [t for t in asyncio.all_tasks() - before if t is not current and not t.done()]


async def test_client_disconnect_mid_stream_cancels_model_calls(env: Env) -> None:
    """Клиент ушёл после первого `delta`: вызов модели отменён и закрыт, живых задач не осталось,
    недописанный ответ не сохранён, реплика владельца — в истории."""
    p = stub(chunks=json_chunks(ANSWER, 60), chunk_delay_s=30.0)  # 1-й кусок — с началом reply
    env.use(p)
    anna = await _person(env.client)
    before = set(asyncio.all_tasks())
    started = time.monotonic()
    raw = await raw_post(anna, "Привет", disconnect=lambda r: b"event: delta" in r.body.encode())
    assert time.monotonic() - started < 3
    assert raw.status == 200 and "event: delta" in raw.body
    assert "event: done" not in raw.body
    assert (p.started, p.cancelled, p.closed) == (1, 1, 1)
    assert _no_new_tasks(before) == []
    assert [i["author"] for i in await history(env, anna)] == ["owner"]


async def test_client_disconnect_during_classifier_cancels_it(env: Env) -> None:
    """Клиент ушёл, пока работает классификатор (до потока): вызов отменён, агент не вызван."""
    p, slow = stub(), SlowSafety(30.0)
    env.use(p, slow)
    anna = await _person(env.client)
    before = set(asyncio.all_tasks())
    raw = await raw_post(anna, "Привет", disconnect_after_s=0.3)
    assert raw.status == chat_api.CLIENT_CLOSED
    assert (slow.started, slow.cancelled) == (1, 1)
    assert p.started == 0
    assert _no_new_tasks(before) == []
    assert [i["author"] for i in await history(env, anna)] == ["owner"]


# --------------------------------------------------------------------------- лимит


async def test_daily_limit_429_with_retry_after_and_owner_message_kept(env: Env) -> None:
    """Исчерпан лимит — 429 `rate_limited` с `Retry-After` (секунды до конца суток UTC); реплика
    сохранена без ответа; 429 не засчитывается."""
    env.box.limit = 2
    anna = await _person(env.client)
    for i in range(2):
        resp, events = await post(env, anna, f"реплика {i}")
        assert resp.status_code == 200 and events[-1][0] == "done"
    resp, _ = await post(env, anna, "третья")
    _error(resp, 429, "rate_limited")
    retry = int(resp.headers["retry-after"])
    assert 1 <= retry <= 86_400
    assert abs(retry - window(datetime.now(UTC))[1]) <= 5
    items = await history(env, anna)
    assert (items[0]["author"], items[0]["text"]) == ("owner", "третья")
    env.box.limit = 3  # 429 не засчитана: третья принятая проходит
    resp, events = await post(env, anna, "четвёртая")
    assert resp.status_code == 200 and events[-1][0] == "done"


async def test_crisis_is_answered_when_limit_exhausted_and_counted(env: Env) -> None:
    """При кризисе 429 не отдаётся (§9.3, §11 п.5), но реплика засчитывается."""
    env.box.limit = 1
    anna = await _person(env.client)
    resp, _ = await post(env, anna, "первая")
    assert resp.status_code == 200
    env.use(stub(category="self_harm"))
    resp, events = await post(env, anna, "мне не хочется жить")
    assert resp.status_code == 200 and events[-1][0] == "crisis"
    env.use(stub())
    env.box.limit = 2  # кризисная засчитана: 2 из 2
    resp, _ = await post(env, anna, "обычная")
    _error(resp, 429, "rate_limited")


class RefusingSafety:
    """Классификатор, который не даёт вердикта (отказ или недоступность модели)."""

    model = "refusing-safety"

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    async def complete_json(self, **kw: Any) -> StructuredResult:
        self.calls += 1
        raise self.exc

    def stream_text(self, **kw: Any) -> Any:  # pragma: no cover — не вызывается
        raise NotImplementedError


async def test_no_verdict_over_limit_goes_to_agent_and_is_counted(env: Env) -> None:
    """АР-34 п.1 (A8-E1): классификатор отказал при исчерпанном лимите — не 429: реплика идёт к
    агенту (первая линия) и засчитывается."""
    env.box.limit = 1
    anna = await _person(env.client)
    assert (await post(env, anna, "первая"))[1][-1][0] == "done"
    p = stub()
    env.use(p, RefusingSafety(LLMRefusal("no")))
    resp, events = await post(env, anna, "я больше не могу")
    assert resp.status_code == 200 and events[-1][0] == "done" and p.started == 1
    env.use(stub())
    env.box.limit = 2  # засчитана: 2 из 2
    _error((await post(env, anna, "обычная"))[0], 429, "rate_limited")


@pytest.mark.parametrize(
    ("agent_exc", "code"),
    [
        (LLMUnavailable("down"), "unavailable"),
        (LLMQuotaError("quota"), "quota"),
        (LLMConfigError("no key"), "internal"),
        (LLMRefusal("no"), "refusal"),
        (LLMBadOutput("cut"), "bad_output"),
    ],
)
async def test_both_lines_unavailable_error_points_to_help(
    env: Env, agent_exc: BaseException, code: str
) -> None:
    """АР-34 п.2 (ревью MAJOR-1): вердикта второй линии нет и агент не ответил — `error` с
    указанием на помощь (номер из списка контактов и раздел «Помощь»)."""
    env.use(stub(chunks=[agent_exc]), RefusingSafety(LLMUnavailable("down")))
    anna = await _person(env.client)
    _, events = await post(env, anna, "Привет")
    name, err = events[-1]
    assert (name, err["code"]) == ("error", code)
    assert "112" in err["message"] and "«Помощь»" in err["message"]


async def test_error_with_verdict_has_no_extra_help_hint(env: Env) -> None:
    """Вердикт «не кризис» есть — сообщение об ошибке обычное (без подмешанной подсказки)."""
    env.use(stub(chunks=[LLMUnavailable("down")]))
    anna = await _person(env.client)
    _, events = await post(env, anna, "Привет")
    assert events[-1][1]["code"] == "unavailable" and "112" not in events[-1][1]["message"]


async def test_classifier_code_error_logged_as_error(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    """Ошибка кода классификатора ответ не блокирует, но пишется в журнал уровнем error."""
    caplog.set_level(logging.INFO)
    env.use(stub(), RefusingSafety(RuntimeError("bug")))
    anna = await _person(env.client)
    _, events = await post(env, anna, "Привет")
    assert events[-1][0] == "done"
    assert any(
        r.levelno == logging.ERROR and "safety screen failed: RuntimeError" in r.getMessage()
        for r in caplog.records
    )


async def test_owner_turn_save_failure_crisis_still_answered(
    env: Env, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ревью MINOR-1: реплику не удалось сохранить (не 409, не «владельца нет») — классификатор
    по самой реплике; кризис — `crisis` с контактами и `message_id: null`, событие записано;
    без кризиса — 500 (исходная ошибка)."""
    anna = await _person(env.client)

    async def broken(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("encryption failed")

    monkeypatch.setattr(chat_repo, "add_owner_turn", broken)
    p = stub(category="self_harm")
    env.use(p)
    _, events = await post(env, anna, "не хочу жить")
    assert [n for n, _ in events] == ["crisis"]
    assert events[0][1]["message_id"] is None and events[0][1]["contacts"] == help_contacts()
    assert p.started == 0
    await env.rt.drain()
    async with engine.connect() as conn:
        types = (
            await conn.execute(
                text("SELECT type::text FROM safety_events WHERE user_id = :u"), {"u": anna.id}
            )
        ).scalars()
        assert list(types) == ["self_harm"]
    env.use(stub())
    with pytest.raises(RuntimeError, match="encryption failed"):
        await env.client.post(API, headers=anna.headers, json={"text": "Привет"})


async def test_safety_events_survive_client_leaving_during_crisis(
    env: Env, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ревью MINOR-3: клиент ушёл, пока пишется кризисный ответ, — событие безопасности всё
    равно записано (фоновая задача, уход клиента её не отменяет)."""
    env.use(stub(category="violence"))
    anna = await _person(env.client)
    real = chat_repo.add_message

    async def slow(*args: Any, **kw: Any) -> Any:
        if args[4] == "agent":
            await asyncio.sleep(1.0)
        return await real(*args, **kw)

    monkeypatch.setattr(chat_repo, "add_message", slow)
    real_events = chat_repo.record_safety_events

    async def slow_events(*args: Any, **kw: Any) -> Any:
        await asyncio.sleep(0.5)
        return await real_events(*args, **kw)

    monkeypatch.setattr(chat_repo, "record_safety_events", slow_events)
    raw = await raw_post(anna, "Он меня бьёт", disconnect_after_s=0.2)
    assert "event: crisis" not in raw.body
    await env.rt.drain()
    async with engine.connect() as conn:
        types = (
            await conn.execute(
                text("SELECT type::text FROM safety_events WHERE user_id = :u"), {"u": anna.id}
            )
        ).scalars()
        assert list(types) == ["violence"]


async def test_safety_events_recorded_when_client_leaves_before_first_byte(
    env: Env, engine: AsyncEngine
) -> None:
    """Ревью р.2 MINOR-3: кризис классификатора — запись `safety_events` стартует сразу после
    вердикта, до потока; клиент ушёл до первого байта ответа — событие всё равно записано."""
    gone = asyncio.Event()

    class LeaveOnLimit(MemoryDailyLimit):
        async def acquire(self, *a: Any, **kw: Any) -> Any:
            gone.set()  # вердикт уже есть, поток ещё не создан
            return await super().acquire(*a, **kw)

    env.use(stub(category="self_harm"))
    env.rt.limiter = LeaveOnLimit()
    anna = await _person(env.client)
    calls: list[bool] = []  # был ли уже шаг лимита (за ним — создание потока)
    real = env.rt.record_safety_events

    def spy(*a: Any, **kw: Any) -> Any:
        calls.append(gone.is_set())
        return real(*a, **kw)

    env.rt.record_safety_events = spy  # type: ignore[method-assign]
    await raw_post(anna, "не хочу жить", disconnect=lambda r: gone.is_set())
    assert calls == [False]  # одна запись, запущена до лимита и до потока
    await env.rt.drain()
    async with engine.connect() as conn:
        types = (
            await conn.execute(
                text("SELECT type::text FROM safety_events WHERE user_id = :u"), {"u": anna.id}
            )
        ).scalars()
        assert list(types) == ["self_harm"]


async def test_unsaved_turn_without_verdict_streams_error_with_help(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ревью р.2 MINOR-2 (A8-E2): реплика не сохранилась и вердикта нет — поток из одного
    `error internal` с указанием на помощь; агент не вызывается."""

    async def broken(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(chat_repo, "add_owner_turn", broken)
    p = stub()
    env.use(p, RefusingSafety(LLMUnavailable("down")))
    anna = await _person(env.client)
    _, events = await post(env, anna, "Привет")
    assert [n for n, _ in events] == ["error"]
    assert events[0][1]["code"] == "internal" and "112" in events[0][1]["message"]
    assert p.started == 0


async def test_unhandled_error_is_500_by_error_schema(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Необработанная ошибка — 500 по схеме `Error`, без текста исключения."""

    async def broken(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("секрет-исключения")

    monkeypatch.setattr(chat_repo, "add_owner_turn", broken)
    anna = await _person(env.client)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(API, headers=anna.headers, json={"text": "Привет"})
    _error(resp, 500, "internal")
    assert "секрет" not in resp.text


async def test_one_compression_task_per_conversation() -> None:
    """Ревью MINOR-5: одна задача сжатия на диалог; запрос во время работы — один повтор после
    неё, без параллельного вызова модели."""
    rt = ChatRuntime()
    conv, other = uuid.uuid4(), uuid.uuid4()
    gate = asyncio.Event()
    running, peak, runs = 0, 0, 0

    async def job() -> None:
        nonlocal running, peak, runs
        running += 1
        peak = max(peak, running)
        runs += 1
        await gate.wait()
        running -= 1

    for _ in range(4):
        rt.compress(conv, job)
    rt.compress(other, job)
    await asyncio.sleep(0.05)
    assert len(rt._compressing) == 2 and peak == 2  # по задаче на диалог
    gate.set()
    await rt.drain()
    assert runs == 3  # conv: первый + один повтор; other: один
    assert rt._compressing == {} and rt.background == set()


async def test_limit_store_failure_breaker_and_process_counter(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    """АР-34 (ревью MINOR-2, тест О-1): хранилище лимита недоступно — ответ идёт; «выключатель»:
    в паузе хранилище не опрашивается; лимит держит запасной счётчик процесса; журнал `error`
    без id."""

    class Broken:
        calls = 0

        async def acquire(self, *a: Any, **kw: Any) -> Any:
            Broken.calls += 1
            raise ConnectionError("redis down")

    caplog.set_level(logging.INFO)
    env.rt.limiter = Broken()
    env.box.limit = 2
    anna = await _person(env.client)
    for i in range(2):
        _, events = await post(env, anna, f"реплика {i}")
        assert events[-1][0] == "done"
    assert Broken.calls == 1  # после первого сбоя — пауза без попыток
    resp, _ = await post(env, anna, "третья")
    _error(resp, 429, "rate_limited")  # лимит не снят: считает запасной счётчик
    errors = [
        r for r in caplog.records if r.levelno == logging.ERROR and "limit store" in r.getMessage()
    ]
    assert len(errors) == 1 and "ConnectionError" in errors[0].getMessage()
    assert str(anna.id) not in caplog.text
    env.rt._limit_down_until = 0.0  # пауза истекла — снова пробуем хранилище
    await post(env, anna, "четвёртая")
    assert Broken.calls == 2


async def test_slow_limit_store_does_not_delay_each_request(env: Env) -> None:
    """Зависший Redis: не дольше `limit_timeout_s` на первый запрос, дальше — без ожидания."""

    class Hanging:
        async def acquire(self, *a: Any, **kw: Any) -> Any:
            await asyncio.sleep(30)

    env.rt.limiter = Hanging()
    env.rt.limit_timeout_s = 0.2
    anna = await _person(env.client)
    t0 = time.monotonic()
    await post(env, anna, "раз")
    first = time.monotonic() - t0
    t0 = time.monotonic()
    await post(env, anna, "два")
    second = time.monotonic() - t0
    assert first < 2 and second < first


async def test_memory_limit_window_and_force() -> None:
    lim = MemoryDailyLimit()
    u = uuid.uuid4()
    day = datetime(2026, 11, 22, 23, 59, 30, tzinfo=UTC)
    assert (await lim.acquire(u, 1, force=False, now=day)).allowed
    no = await lim.acquire(u, 1, force=False, now=day)
    assert (no.allowed, no.count, no.retry_after_s) == (False, 1, 30)
    assert (await lim.acquire(u, 1, force=True, now=day)).count == 2
    assert (await lim.acquire(u, 1, force=False, now=day + timedelta(minutes=1))).allowed
    assert window(datetime(2026, 11, 22, 23, 59, 59, 999_000, tzinfo=UTC))[1] == 1


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.mark.skipif(shutil.which("redis-server") is None, reason="нет redis-server")
async def test_redis_limit_is_atomic_and_expires() -> None:
    """Хранилище продуктива — Redis: проверка и увеличение одним скриптом, ключ с TTL."""
    from redis.asyncio import Redis

    port = _free_port()
    proc = await asyncio.create_subprocess_exec(
        "redis-server",
        "--port",
        str(port),
        "--save",
        "",
        "--appendonly",
        "no",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    client = Redis(host="127.0.0.1", port=port)
    try:
        for _ in range(50):
            try:
                await client.ping()
                break
            except Exception:
                await asyncio.sleep(0.05)
        lim = RedisDailyLimit(client)
        u = uuid.uuid4()
        results = await asyncio.gather(*(lim.acquire(u, 5, force=False) for _ in range(20)))
        assert sum(r.allowed for r in results) == 5
        assert all(r.count == 5 for r in results if not r.allowed)
        forced = await lim.acquire(u, 5, force=True)
        assert (forced.allowed, forced.count) == (True, 6)
        keys = await client.keys("feelings:chat:daily:*")
        assert len(keys) == 1 and str(u).encode() in keys[0]
        ttl = await client.ttl(keys[0])
        assert 0 < ttl <= 86_400 + 3600
    finally:
        await client.aclose()
        proc.terminate()
        await asyncio.wait_for(proc.wait(), 5)


# --------------------------------------------------------------------------- сжатие в фоне


async def test_history_compression_runs_in_background(env: Env, engine: AsyncEngine) -> None:
    """После `done` — сжатие истории в фоне (повтор, пока `more`); сводка — шифротекст."""
    env.rt.compression = CompressionPolicy(trigger_turns=7, keep_turns=6, batch_turns=2)
    anna = await _person(env.client)
    for i in range(5):
        await post(env, anna, f"реплика {i}")
        await env.rt.drain()
    async with engine.connect() as conn:
        summary = (
            await conn.execute(
                text(
                    "SELECT c.summary_enc FROM conversations c JOIN agents a ON a.id = c.agent_id "
                    "WHERE a.user_id = :u"
                ),
                {"u": anna.id},
            )
        ).scalar_one()
    assert summary is not None  # сводка записана (шифротекст)
    assert "выходные".encode() not in summary


async def test_compression_failure_is_invisible(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    def fail(payload: dict[str, Any]) -> dict[str, Any]:
        raise LLMUnavailable("down")

    env.use(stub(summary=fail))
    env.rt.compression = CompressionPolicy(trigger_turns=7, keep_turns=6)
    anna = await _person(env.client)
    caplog.set_level(logging.INFO, logger="app.api.chat")
    for i in range(4):
        _, events = await post(env, anna, f"реплика {i}")
        assert events[-1][0] == "done"
        await env.rt.drain()
    assert any("status=error" in r.getMessage() for r in caplog.records)


def test_runtime_requires_keep_turns_at_least_6() -> None:
    """Тест 2.1 О-5: иначе классификатор после сжатия видит меньше трёх реплик владельца."""
    with pytest.raises(ValueError, match="keep_turns"):
        ChatRuntime(compression=CompressionPolicy(trigger_turns=10, keep_turns=5))


# --------------------------------------------------------------------------- история


async def test_history_pages_newest_first_with_opaque_cursor(env: Env) -> None:
    anna = await _person(env.client)
    sent = [f"реплика {i}" for i in range(4)]
    for body in sent:
        await post(env, anna, body)
    resp = await env.client.get(API, headers=anna.headers, params={"limit": 3})
    assert resp.headers["cache-control"] == "no-store"
    page = resp.json()
    assert_matches(page["items"][0], "ChatMessage")
    pages = [page["items"]]
    # Реплика после выдачи курсора на следующих страницах не появляется.
    await post(env, anna, "новая")
    while page["next_cursor"] is not None:
        page = (
            await env.client.get(
                API, headers=anna.headers, params={"limit": 3, "cursor": page["next_cursor"]}
            )
        ).json()
        pages.append(page["items"])
    assert [len(p) for p in pages] == [3, 3, 2]
    flat = [i for p in pages for i in p]
    assert [i["author"] for i in flat] == ["agent", "owner"] * 4
    assert [i["text"] for i in flat if i["author"] == "owner"] == sent[::-1]
    stamps = [(i["created_at"], i["id"]) for i in flat]
    assert stamps == sorted(stamps, reverse=True)
    assert len({i["id"] for i in flat}) == 8
    fresh = await history(env, anna, limit=2)
    assert fresh[1]["text"] == "новая"


async def test_history_default_limit_and_bounds(env: Env) -> None:
    anna = await _person(env.client)
    page = (await env.client.get(API, headers=anna.headers)).json()
    assert page == {"items": [], "next_cursor": None}
    for bad in ("0", "101", "x"):
        _error(
            await env.client.get(API, headers=anna.headers, params={"limit": bad}),
            422,
            "validation_error",
        )


async def test_foreign_or_forged_cursor_is_422(env: Env) -> None:
    anna, olga = await _person(env.client), await _person(env.client, "Ольга", "Пётр")
    for i in range(3):
        await post(env, anna, f"реплика {i}")
    cursor = (await env.client.get(API, headers=anna.headers, params={"limit": 1})).json()[
        "next_cursor"
    ]
    assert cursor
    head, sig = cursor.split(".")
    forged = chat_api._b64(f"{datetime.now(UTC).isoformat()}|{uuid.uuid4()}".encode())
    for bad in (cursor + "x", f"{forged}.{sig}", "abc", "", "a.b.c", head):
        _error(
            await env.client.get(API, headers=anna.headers, params={"cursor": bad}),
            422,
            "validation_error",
        )
    # Курсор Анны у Ольги не разбирается (подпись привязана к владельцу).
    _error(
        await env.client.get(API, headers=olga.headers, params={"cursor": cursor}),
        422,
        "validation_error",
    )


async def test_history_marks_undecryptable_text(env: Env, engine: AsyncEngine) -> None:
    """Нерасшифрованный текст — `UnavailableText`, реплика не пропускается и ответ не падает."""
    anna = await _person(env.client)
    await post(env, anna, "Привет")
    async with engine.begin() as conn:
        conv = (
            await conn.execute(
                text(
                    "SELECT c.id FROM conversations c JOIN agents a ON a.id = c.agent_id "
                    "WHERE a.user_id = :u"
                ),
                {"u": anna.id},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO conversation_messages (id, conversation_id, author, text_enc) "
                "VALUES (:i, :c, 'owner', :t)"
            ),
            {"i": uuid.uuid4(), "c": conv, "t": b"\x00broken"},
        )
    items = await history(env, anna)
    assert len(items) == 3
    broken = items[0]
    assert broken["text"]["unavailable"] is True and isinstance(broken["text"]["reason"], str)
    assert_matches(broken, "ChatMessage")


# --------------------------------------------------------------------------- изоляция


async def test_partner_does_not_see_or_get_other_chat(env: Env) -> None:
    """Партнёр по активной паре не видит личный чат владельца ни в истории, ни во входе своего
    агента и классификатора (ТЗ §2 п.4, §6)."""
    p = stub()
    env.use(p)
    anna, igor = await _person(env.client), await _person(env.client, "Игорь", "Анна")
    token = await _create(env.client, anna)  # type: ignore[arg-type]
    resp = await env.client.post(f"/api/v1/invites/{token}/accept", headers=igor.headers)
    assert resp.status_code == 200, resp.text
    secret = mark("anna-secret")
    await post(env, anna, secret)
    assert await history(env, igor) == []
    p.requests.clear()
    p.stream_requests.clear()
    _, events = await post(env, igor, "Как дела?")
    assert events[-1][0] == "done"
    model_input = json.dumps([p.requests, p.stream_requests], ensure_ascii=False)
    assert secret not in model_input
    assert all(secret not in i["text"] for i in await history(env, igor))
    # Курсор Анны Игорю тоже не годится.
    await post(env, anna, "ещё")
    cursor = (await env.client.get(API, headers=anna.headers, params={"limit": 1})).json()[
        "next_cursor"
    ]
    _error(
        await env.client.get(API, headers=igor.headers, params={"cursor": cursor}),
        422,
        "validation_error",
    )


# --------------------------------------------------------------------------- журналы


async def test_logs_and_audit_have_no_texts(
    env: Env, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Ни реплика владельца, ни ответ агента не попадают в журналы, `audit_log` и
    `safety_events` (обычный ответ, кризис, ошибка); в журнале — имя события."""
    caplog.set_level(logging.DEBUG)
    anna = await _person(env.client)
    secret_reply = mark("reply")
    marks = [secret_reply]
    env.use(stub({**ANSWER, "reply": secret_reply}))
    m = mark("normal")
    marks.append(m)
    await post(env, anna, m)
    env.use(stub(category="self_harm"))
    m = mark("crisis")
    marks.append(m)
    await post(env, anna, m)
    env.use(stub(chunks=[LLMRefusal("no")]))
    m = mark("error")
    marks.append(m)
    await post(env, anna, m)
    await env.rt.drain()
    logged = "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)
    assert "chat reply: event=done" in logged and "event=crisis" in logged
    assert "event=error code=refusal" in logged
    for m in marks:
        assert m not in logged
        assert m.split("-")[-1] not in logged
    async with engine.connect() as conn:
        for table in ("audit_log", "safety_events"):
            dump = (
                await conn.execute(
                    text(f"SELECT coalesce(string_agg(t::text, ' '), '') FROM {table} t")
                )
            ).scalar_one()
            assert all(m not in dump for m in marks), table


# --------------------------------------------------------------------------- 401/403/422/409


async def test_auth_onboarding_and_body_checks(env: Env) -> None:
    p = stub()
    env.use(p)
    ghost = issue_token(uuid.uuid4(), SECRET, 600)
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": f"Bearer {ghost}"}):
        _error(await env.client.post(API, headers=headers, json={"text": "x"}), 401, "unauthorized")
        _error(await env.client.get(API, headers=headers), 401, "unauthorized")
        # 2.0v (A2): контакты помощи — публичный путь: без сессии и с негодным токеном — 200.
        resp = await env.client.get("/api/v1/help/contacts", headers=headers)
        assert resp.status_code == 200 and resp.json() == help_contacts()
        assert resp.headers["cache-control"] == "no-store"
    fresh = await _login(env.client)  # вход без онбординга
    _error(
        await env.client.post(API, headers=fresh.headers, json={"text": "x"}),
        403,
        "onboarding_required",
    )
    _error(await env.client.get(API, headers=fresh.headers), 403, "onboarding_required")
    # Контакты помощи — без онбординга и без подписки.
    resp = await env.client.get("/api/v1/help/contacts", headers=fresh.headers)
    assert resp.status_code == 200
    anna = await _person(env.client)
    for body in (
        {},
        {"text": ""},
        {"text": "   \n\t "},
        {"text": "я" * 4001},
        {"text": 5},
        {"text": "x", "extra": 1},
        [],
    ):
        resp = await env.client.post(API, headers=anna.headers, json=body)
        _error(resp, 422, "validation_error")
        assert "я" * 50 not in resp.text
    assert p.requests == [] and p.stream_requests == []
    assert await history(env, anna) == []
    resp, events = await post(env, anna, "я" * 4000)
    assert resp.status_code == 200 and events[-1][0] == "done"


async def test_conflict_is_409_and_nothing_saved(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """40P01/40001 в транзакции сохранения реплики — 409 `conflict_retry`: ни диалога, ни
    реплики; модель не вызывается; повтор проходит."""
    p = stub()
    env.use(p)
    anna = await _person(env.client)
    real = chat_repo._insert_message

    async def conflict(s: AsyncSession, *args: Any, **kw: Any) -> Any:
        await real(s, *args, **kw)
        await s.execute(
            text("DO $$ BEGIN RAISE EXCEPTION 'conflict' USING ERRCODE = '40P01'; END $$")
        )

    monkeypatch.setattr(chat_repo, "_insert_message", conflict)
    resp = await env.client.post(API, headers=anna.headers, json={"text": "Привет"})
    _error(resp, 409, "conflict_retry")
    assert p.requests == [] and p.stream_requests == []
    monkeypatch.undo()
    assert await history(env, anna) == []
    resp, events = await post(env, anna, "Привет")
    assert events[-1][0] == "done"


# --------------------------------------------------------------------------- контакты помощи


async def test_help_contacts_only_112_until_operator_check(env: Env) -> None:
    """АР-33: до проверки оператором — только 112 и пометка «не проверено»; в продуктив без
    проверенного списка сервис не стартует."""
    anna = await _login(env.client)
    resp = await env.client.get("/api/v1/help/contacts", headers=anna.headers)
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    contacts = resp.json()
    for c in contacts:
        assert_matches(c, "HelpContact")
    assert [c["phone"] for c in contacts] == ["112"]
    assert HELP_CONTACTS.verified_by_operator is False
    for env_name in ("dev", "test", "local"):
        check_help_contacts_config(Settings(env=env_name))
    with pytest.raises(ConfigError, match="verified"):
        check_help_contacts_config(Settings(env="prod"))

"""2.1 этап 2, A8: независимая проверка `POST/GET /chat/messages` (SSE), `GET /help/contacts`,
дневного лимита (коммит 9e5ec8c; контракт `/chat/messages`, `/help/contacts`; АР-30, АР-32, АР-33).

Хелперы исполнителя (`tests/test_chat_api.py`) не используются. Вход — по initData, онбординг —
по HTTP; модель — своя заглушка `FakeModel` (tests/test_chat_a8.py), сеть к OpenRouter не нужна.
SSE и отключение клиента проверяются собственным ASGI-драйвером (`call`): он сам выдаёт
`http.request`/`http.disconnect` и записывает каждый кусок ответа со временем. Данные
выдуманные; тексты помечены метками, утечки ищутся поиском меток. Дефекты —
`xfail(strict=True)` (docs/reviews/2.1-test.md, «Этап 2, раунд 1»)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
    MutableMapping,
    Sequence,
)
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.ai.llm import (
    LLMAuthError,
    LLMConfigError,
    LLMModelNotFound,
    LLMQuotaError,
    LLMRefusal,
    LLMRequestError,
    LLMUnavailable,
    StructuredResult,
)
from app.ai.summary import CompressionPolicy
from app.api import chat as chat_api
from app.api.chat import ChatRuntime
from app.config import ConfigError, Settings, get_settings
from app.core import crypto
from app.core.crypto import Keyring, generate_key_entry, parse_keyring
from app.core.limits import MemoryDailyLimit, RedisDailyLimit
from app.core.telegram import sign_init_data
from app.help import (
    HELP_CONTACTS,
    HELP_CONTACTS_PATH,
    check_help_contacts_config,
    help_contacts,
    parse_help_contacts,
)
from app.main import app
from app.repositories import chat as chat_repo
from tests.contract import assert_matches
from tests.test_chat_a8 import FakeModel, _answer, _chunks, models_of

SM = async_sessionmaker[AsyncSession]
V1 = "/api/v1"
CHAT = f"{V1}/chat/messages"
BOT_TOKEN = "777002:A8-chat-bot-token-not-real"
SESSION_SECRET = "a8-chat-session-secret-" + "z" * 24
REPLY = "Слышу, как тебе обидно. Что задело сильнее всего?"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
EVENT_SCHEMAS = {
    "delta": "ChatStreamDelta",
    "done": "ChatStreamDone",
    "crisis": "ChatStreamCrisis",
    "error": "ChatStreamError",
}
FINAL = ("done", "crisis", "error")
CRISIS = {"category": "self_harm", "secondary": []}


# =============================================================== окружение


@dataclass
class Env:
    limit: int = 100
    keepalive_s: float = 15.0
    env: str = "test"

    def settings(self) -> Settings:
        return Settings(
            telegram_bot_token=SecretStr(BOT_TOKEN),
            session_secret=SecretStr(SESSION_SECRET),
            session_ttl_s=900,
            bot_username="A8_Chat_bot",
            chat_daily_limit=self.limit,
            chat_keepalive_s=self.keepalive_s,
            env=self.env,
        )


@dataclass
class World:
    http: httpx.AsyncClient
    env: Env
    rt: ChatRuntime
    agent: FakeModel
    clf: FakeModel
    summ: FakeModel
    keys: Keyring


@pytest.fixture
async def w(sessionmaker: SM) -> AsyncIterator[World]:
    keys = parse_keyring(generate_key_entry("a8c"), "a8c")
    env = Env()
    agent = FakeModel(stream=_chunks(_answer(REPLY)))
    clf = FakeModel()
    summ = FakeModel(summary={"summary": "сводка"})
    rt = ChatRuntime(models=models_of(agent, clf, summ), limiter=MemoryDailyLimit())
    saved = (app.state.sessionmaker, app.state.keyring, app.state.chat)
    app.state.sessionmaker, app.state.keyring, app.state.chat = sessionmaker, keys, rt
    app.dependency_overrides[get_settings] = env.settings
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://a8") as c:
        yield World(c, env, rt, agent, clf, summ, keys)
    await rt.aclose()
    app.dependency_overrides.clear()
    app.state.sessionmaker, app.state.keyring, app.state.chat = saved


@dataclass
class Person:
    uid: uuid.UUID
    tg: int
    token: str
    label: str

    @property
    def h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


async def enter(w: World, label: str, *, onboard: bool = True, tg: int | None = None) -> Person:
    tg = tg or 720_000_000_000 + secrets.randbelow(99_999_999_999)
    init = sign_init_data(
        {
            "auth_date": str(int(time.time())),
            "user": json.dumps({"id": tg, "first_name": label}, ensure_ascii=False),
        },
        BOT_TOKEN,
    )
    r = await w.http.post(f"{V1}/auth/telegram", json={"init_data": init})
    assert r.status_code == 200, r.text
    body = r.json()
    p = Person(uuid.UUID(body["me"]["id"]), tg, body["access_token"], label)
    if onboard:
        r = await w.http.put(
            f"{V1}/me/onboarding",
            headers=p.h,
            json={
                "name": f"{label}-name",
                "address_form": "на ты",
                "partner_name": f"{label}-partner",
                "timezone": "Europe/Moscow",
                "consent_terms": True,
                "consent_data": True,
                "consent_sensitive": True,
                "age_confirmed": True,
            },
        )
        assert r.status_code == 200, r.text
    return p


async def su(engine: AsyncEngine, sql: str, **params: object) -> Any:
    async with engine.begin() as c:
        res = await c.execute(text(sql), params)
        return res.all() if res.returns_rows else None


async def turns_of(engine: AsyncEngine, w: World, uid: uuid.UUID) -> list[tuple[str, str]]:
    rows = await su(
        engine,
        "SELECT m.id, m.author::text AS a, m.text_enc FROM conversation_messages m "
        "JOIN conversations c ON c.id = m.conversation_id JOIN agents g ON g.id = c.agent_id "
        "WHERE g.user_id = :u ORDER BY m.created_at, m.id",
        u=uid,
    )
    return [(r.a, w.keys.open(crypto.CONVERSATION_MESSAGE_TEXT, r.id, r.text_enc)) for r in rows]


# =============================================================== ASGI-драйвер SSE


@dataclass
class Resp:
    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    chunks: list[tuple[float, str]] = field(default_factory=list)
    finished: bool = False

    @property
    def body(self) -> str:
        return "".join(c for _, c in self.chunks)

    def json(self) -> Any:
        return json.loads(self.body)


async def call(
    method: str,
    path: str,
    *,
    headers: Mapping[str, str],
    body: Any = None,
    disconnect: asyncio.Event | None = None,
    on_chunk: Callable[[Resp], None] | None = None,
    timeout_s: float = 20,
    after_gone: str = "raise",
) -> Resp:
    """Запрос к приложению напрямую по ASGI: тело — одним ``http.request``, дальше ``receive``
    ждёт ``disconnect`` (тогда ``http.disconnect``). Кусок ответа — в ``resp.chunks``."""
    raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
    hdrs = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    hdrs.append((b"content-type", b"application/json"))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": hdrs,
        "client": ("127.0.0.1", 50000),
        "server": ("a8", 80),
        "state": {},
    }
    sent_body = False
    gone = disconnect or asyncio.Event()
    resp = Resp()
    start = time.monotonic()

    async def receive() -> dict[str, Any]:
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            resp.status = message["status"]
            resp.headers = {k.decode().lower(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            if gone.is_set():
                if after_gone == "drop":  # как uvicorn: запись после обрыва молча теряется
                    return
                raise OSError("клиент ушёл")  # ASGI 2.4
            chunk = message.get("body", b"").decode()
            if chunk:
                resp.chunks.append((time.monotonic() - start, chunk))
                if on_chunk is not None:
                    on_chunk(resp)
            if not message.get("more_body", False):
                resp.finished = True

    try:
        await asyncio.wait_for(app(scope, receive, send), timeout_s)
    except OSError:
        resp.finished = False  # запись в закрытое соединение (ASGI 2.4): сервер это глушит
    return resp


def events(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Разбор SSE строго по контракту: блок — `event:` и `data:` в одну строку; комментарии
    `:` пропускаются; `id:`/`retry:` запрещены."""
    out: list[tuple[str, dict[str, Any]]] = []
    assert body.endswith("\n\n"), "поток не закончен пустой строкой"
    for block in body[:-2].split("\n\n"):
        lines = [ln for ln in block.split("\n") if not ln.startswith(":")]
        if not lines:
            continue
        assert len(lines) == 2, block
        assert lines[0].startswith("event: ") and lines[1].startswith("data: "), block
        name = lines[0][len("event: ") :]
        data = json.loads(lines[1][len("data: ") :])
        assert name in EVENT_SCHEMAS, name
        assert_matches(data, EVENT_SCHEMAS[name])
        out.append((name, data))
    finals = [n for n, _ in out if n in FINAL]
    assert len(finals) == 1 and out[-1][0] in FINAL, [n for n, _ in out]
    return out


async def post(w: World, p: Person, body: str, **kw: Any) -> Resp:
    return await call("POST", CHAT, headers=p.h, body={"text": body}, **kw)


def stream_ok(r: Resp) -> list[tuple[str, dict[str, Any]]]:
    assert r.status == 200, r.body
    assert r.headers["content-type"].replace(" ", "").lower() == "text/event-stream;charset=utf-8"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers.get("x-accel-buffering") == "no"
    assert r.finished
    return events(r.body)


def delta_text(evs: Sequence[tuple[str, dict[str, Any]]]) -> str:
    return "".join(d["text"] for n, d in evs if n == "delta")


# =============================================================== 1. Формат SSE и done


async def test_done_stream_format_and_saved_text(w: World, engine: AsyncEngine) -> None:
    """Контракт `POST /chat/messages` (АР-32): `event:`/`data:` в одну строку, `delta`*, ровно
    одно `done`, склейка `delta` == сохранённая реплика агента; `message_id` — её id; заголовки
    `no-store`, `X-Accel-Buffering: no`; ни `id:`, ни `retry:` (ФТ-05, ТЗ §10)."""
    w.agent.stream = _chunks(_answer('Строка 1\nСтрока 2 "кавычки" \\ и эмодзи 🙂 — ' + REPLY))
    anna = await enter(w, "Анна")
    r = await post(w, anna, "Мне обидно")
    evs = stream_ok(r)
    assert "\nid:" not in r.body and "\nretry:" not in r.body and not r.body.startswith("id:")
    assert [n for n, _ in evs][-1] == "done" and sum(n == "delta" for n, _ in evs) > 1
    done = evs[-1][1]
    saved = await turns_of(engine, w, anna.uid)
    assert saved[-2] == ("owner", "Мне обидно")
    assert saved[-1] == ("agent", delta_text(evs))
    hist = (await w.http.get(CHAT, headers=anna.h)).json()
    assert hist["items"][0]["id"] == done["message_id"]
    assert hist["items"][0]["text"] == delta_text(evs)


async def test_first_delta_before_answer_end(w: World) -> None:
    """ТЗ §10 «первый символ», карточка 2.1 кр.4: первый `delta` уходит клиенту задолго до конца
    ответа (заглушка с задержкой между кусками)."""
    w.agent.stream = _chunks(_answer(REPLY + " " + "и ещё " * 30), size=4)
    w.agent.chunk_delay_s = 0.02
    anna = await enter(w, "Анна")
    r = await post(w, anna, "привет")
    evs = stream_ok(r)
    first = next(t for t, c in r.chunks if "event: delta" in c)
    last = r.chunks[-1][0]
    assert first < last / 3, (first, last)
    assert evs[-1][0] == "done"


async def test_keepalive_comment_between_events(w: World) -> None:
    """Контракт: строки-комментарии `:` — поддержание соединения; разбор их пропускает."""
    w.env.keepalive_s = 0.05
    w.agent.first_delay_s = 0.3
    anna = await enter(w, "Анна")
    r = await post(w, anna, "привет")
    assert ": keep-alive" in r.body
    assert stream_ok(r)[-1][0] == "done"


# =============================================================== 2. Реплика владельца до модели


class SpyFirst(FakeModel):
    """Классификатор, который при вызове проверяет, что реплика владельца уже в БД."""

    seen_saved: list[bool]
    check: Callable[[], Awaitable[bool]] | None

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.seen_saved = []
        self.check = None

    async def complete_json(self, **kw: Any) -> StructuredResult:
        if self.check is not None:
            self.seen_saved.append(await self.check())
        return await super().complete_json(**kw)


ERRORS: list[tuple[str, list[Any], str]] = [
    ("refusal", [LLMRefusal("no")], "refusal"),
    ("unavailable", [LLMUnavailable("503")], "unavailable"),
    ("quota", [LLMQuotaError("spend")], "quota"),
    ("bad_output", ["это не json"], "bad_output"),
    ("after_chunks", [*_chunks(_answer(REPLY))[:8], LLMUnavailable("reset")], "unavailable"),
    ("config", [LLMConfigError("key")], "internal"),
    ("auth", [LLMAuthError("401")], "internal"),
    ("bad_request", [LLMRequestError("400")], "internal"),
    ("model_not_found", [LLMModelNotFound("404")], "internal"),
    ("code_bug", [RuntimeError("bug")], "internal"),
]


@pytest.mark.parametrize(("name", "stream", "code"), ERRORS, ids=[e[0] for e in ERRORS])
async def test_each_error_code_keeps_owner_turn(
    name: str, stream: list[Any], code: str, w: World, engine: AsyncEngine
) -> None:
    """Контракт `ChatStreamError`/`ChatErrorCode` (АР-32 п.4), ТЗ §10 «текст не теряется»,
    АР-20: реплика владельца сохранена ДО вызова модели и остаётся при каждой ошибке; ровно одно
    `error` с кодом из перечня; показанные до ошибки куски не сохраняются; ответа агента нет."""
    anna = await enter(w, "Анна")
    spy = SpyFirst()
    owner_text = f"реплика-{name}"

    async def saved() -> bool:
        return ("owner", owner_text) in await turns_of(engine, w, anna.uid)

    spy.check = saved
    w.rt.models = models_of(FakeModel(stream=stream), spy, w.summ)
    evs = stream_ok(await post(w, anna, owner_text))
    assert evs[-1][0] == "error" and evs[-1][1]["code"] == code
    assert spy.seen_saved == [True]
    history = await turns_of(engine, w, anna.uid)
    assert history == [("owner", owner_text)]
    assert evs[-1][1]["message"].strip()
    assert owner_text not in json.dumps(evs[-1][1], ensure_ascii=False)


async def test_models_not_configured_is_internal_error(
    w: World, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """АР-32 п.4: сервис не настроен (нет ключа модели) — `error internal`, реплика сохранена,
    сеть не используется (создание моделей подменено ошибкой настройки)."""

    async def boom(*a: Any, **k: Any) -> Any:
        raise LLMConfigError("нет ключа")

    monkeypatch.setattr(chat_api, "openrouter_models_async", boom)
    w.rt.models = None
    anna = await enter(w, "Анна")
    evs = stream_ok(await post(w, anna, "привет"))
    assert evs == [("error", evs[-1][1])] and evs[-1][1]["code"] == "internal"
    assert await turns_of(engine, w, anna.uid) == [("owner", "привет")]


async def test_validation_and_access(w: World, engine: AsyncEngine) -> None:
    """Контракт: 401 без входа, 403 без онбординга, 422 — пустой/пробелы/длиннее 4000/лишнее
    поле/не строка; ничего не сохранено, модель не вызвана."""
    r = await w.http.post(CHAT, json={"text": "x"})
    assert r.status_code == 401
    raw = await enter(w, "Без", onboard=False)
    r = await w.http.post(CHAT, headers=raw.h, json={"text": "x"})
    assert r.status_code == 403 and r.json()["code"] == "onboarding_required"
    anna = await enter(w, "Анна")
    for bad in ({"text": ""}, {"text": "   \n\t"}, {"text": "я" * 4001}, {}, {"text": 5},
                {"text": "x", "extra": 1}):  # fmt: skip
        r = await w.http.post(CHAT, headers=anna.h, json=bad)
        assert r.status_code == 422, bad
    r = await w.http.post(CHAT, headers=anna.h, json={"text": "я" * 4000})
    assert r.status_code == 200
    assert await turns_of(engine, w, anna.uid) == [("owner", "я" * 4000), ("agent", REPLY)]
    assert len(w.agent.kinds("stream")) == 1


# =============================================================== 3. Кризис всегда с контактами


async def _safety(engine: AsyncEngine, uid: uuid.UUID) -> list[tuple[Any, ...]]:
    rows = await su(engine, "SELECT * FROM safety_events WHERE user_id = :u", u=uid)
    return [tuple(r) for r in rows]


def assert_crisis(evs: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    name, data = evs[-1]
    assert name == "crisis"
    assert data["contacts"] == help_contacts() and len(data["contacts"]) >= 1
    assert data["text"].strip()
    return data


async def test_crisis_before_agent(w: World, engine: AsyncEngine) -> None:
    """ТЗ §9.3, АР-30: кризис от классификатора — до агента (агент не вызывается), `crisis` без
    `delta`, `replace=false`, контакты = `GET /help/contacts`; сохранена реплика с текстом
    протокола; `safety_events` — только тип (столбцы id, user_id, type, created_at)."""
    w.clf.verdict = CRISIS
    anna = await enter(w, "Анна")
    evs = stream_ok(await post(w, anna, "мне не хочется жить"))
    data = assert_crisis(evs)
    assert [n for n, _ in evs] == ["crisis"] and data["replace"] is False
    assert w.agent.kinds("stream") == []
    saved = await turns_of(engine, w, anna.uid)
    assert saved == [("owner", "мне не хочется жить"), ("agent", data["text"])]
    assert data["message_id"] is not None
    cols = await su(
        engine,
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'safety_events'",
    )
    assert {c[0] for c in cols} == {"id", "user_id", "type", "created_at"}
    events_ = await su(
        engine, "SELECT type::text FROM safety_events WHERE user_id = :u", u=anna.uid
    )
    assert [e[0] for e in events_] == ["self_harm"]
    r = await w.http.get(f"{V1}/help/contacts", headers=anna.h)
    assert r.json() == data["contacts"]


async def test_crisis_from_agent(w: World, engine: AsyncEngine) -> None:
    """ТЗ §9.3: кризис распознал агент — его ответ показан кусками, `crisis` с `replace=false`,
    `text` начинается с показанного и сохраняется целиком."""
    w.agent.stream = _chunks(_answer("Мне очень жаль, что тебе так тяжело.", level=5,
                                     crisis_type="self_harm"))  # fmt: skip
    anna = await enter(w, "Анна")
    evs = stream_ok(await post(w, anna, "плохо"))
    data = assert_crisis(evs)
    shown = delta_text(evs)
    assert shown and data["replace"] is False and data["text"].startswith(shown.strip())
    assert (await turns_of(engine, w, anna.uid))[-1] == ("agent", data["text"])


async def test_crisis_when_context_build_fails(
    w: World, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ТЗ §9.3 «всегда»: сборка контекста упала — классификатор видит саму реплику, кризис идёт
    с контактами; без кризиса — `error internal`, реплика сохранена."""

    async def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("context")

    monkeypatch.setattr(chat_api, "build_context", boom)
    w.clf.verdict = CRISIS
    anna = await enter(w, "Анна")
    evs = stream_ok(await post(w, anna, "КРИЗИСa8"))
    assert_crisis(evs)
    [call_] = w.clf.kinds("safety")
    assert "КРИЗИСa8" in call_["messages"][0][1]
    w.clf.verdict = {"category": "none", "secondary": []}
    evs = stream_ok(await post(w, anna, "обычная"))
    assert evs[-1] == ("error", {"code": "internal", "message": evs[-1][1]["message"]})
    assert ("owner", "обычная") in await turns_of(engine, w, anna.uid)


async def test_crisis_when_saving_fails(
    w: World, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Контракт `ChatStreamCrisis.message_id: null`: ответ не сохранился (сбой БД) и событие не
    записалось — `crisis` с контактами всё равно; поток не падает."""

    async def boom(*a: Any, **k: Any) -> Any:
        raise OSError("db down")

    w.clf.verdict = CRISIS
    anna = await enter(w, "Анна")
    monkeypatch.setattr(chat_repo, "add_message", boom)
    monkeypatch.setattr(chat_repo, "record_safety_events", boom)
    evs = stream_ok(await post(w, anna, "плохо"))
    data = assert_crisis(evs)
    assert data["message_id"] is None


async def test_crisis_when_limit_exhausted(w: World, engine: AsyncEngine) -> None:
    """ТЗ §9.3, §11 п.5, АР-32 п.5: при исчерпанном лимите кризис — 200 `crisis` (не 429) и
    засчитывается; обычная реплика после — 429."""
    w.env.limit = 1
    anna = await enter(w, "Анна")
    assert stream_ok(await post(w, anna, "раз"))[-1][0] == "done"
    w.clf.verdict = CRISIS
    for _ in range(2):
        assert_crisis(stream_ok(await post(w, anna, "плохо")))
    w.clf.verdict = {"category": "none", "secondary": []}
    r = await post(w, anna, "три")
    assert r.status == 429


async def test_agent_reply_save_failure_is_single_error(
    w: World, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Контракт `ChatErrorCode.internal` («не удалось сохранить ответ агента»): куски показаны,
    сохранение упало — ровно одно `error internal`, `done` нет, реплика владельца на месте."""

    async def boom(*a: Any, **k: Any) -> Any:
        raise OSError("db down")

    anna = await enter(w, "Анна")
    monkeypatch.setattr(chat_repo, "add_message", boom)
    evs = stream_ok(await post(w, anna, "привет"))
    assert delta_text(evs) == REPLY
    assert evs[-1][0] == "error" and evs[-1][1]["code"] == "internal"
    assert await turns_of(engine, w, anna.uid) == [("owner", "привет")]


async def test_classifier_refusal_over_limit_is_not_bare_429(w: World) -> None:
    """ТЗ §9.3 «кризисный ответ — всегда», §2 п.7, АР-30/32: вторая линия не дала вердикта
    (модель классификатора отказалась — типично именно на текстах о самоповреждении), лимит
    исчерпан. Сейчас — 429 «продолжим завтра»: первая линия (агент) не вызывается, контактов
    помощи нет. Ожидается: без вердикта классификатора 429 не отдаётся (реплика идёт к агенту как
    при `force`) или ответ 429 несёт контакты помощи."""
    w.env.limit = 1
    anna = await enter(w, "Анна")
    assert stream_ok(await post(w, anna, "раз"))[-1][0] == "done"
    w.clf.verdict = LLMRefusal("классификатор отказался")
    r = await post(w, anna, "я больше не могу, хочу всё закончить")
    assert r.status == 200 or "contacts" in r.body


async def test_validation_error_does_not_echo_or_log_text(
    w: World, caplog: pytest.LogCaptureFixture
) -> None:
    """CLAUDE.md, ТЗ §7: отклонённая реплика (длиннее 4000) не возвращается в ответе 422 и не
    пишется в журнал."""
    caplog.set_level(logging.DEBUG)
    anna = await enter(w, "Анна")
    secret = "ДЛИННЫЙСЕКРЕТ" + "я" * 4000
    r = await w.http.post(CHAT, headers=anna.h, json={"text": secret})
    assert r.status_code == 422 and "ДЛИННЫЙСЕКРЕТ" not in r.text
    assert "ДЛИННЫЙСЕКРЕТ" not in caplog.text


# =============================================================== 4. Лимит


async def test_rate_limited_429_and_only_accepted_counted(w: World, engine: AsyncEngine) -> None:
    """Контракт `RateLimited` (АР-32 п.5, ТЗ §10): 429 `rate_limited` + `Retry-After` (целое ≥ 1,
    не дольше суток), `no-store`; реплика владельца сохранена, агент не вызван; 429 не
    засчитывается — после трёх 429 увеличение лимита на 1 пропускает ровно одну реплику."""
    w.env.limit = 2
    anna = await enter(w, "Анна")
    for i in range(2):
        assert stream_ok(await post(w, anna, f"ok{i}"))[-1][0] == "done"
    calls_before = len(w.agent.kinds("stream"))
    for i in range(3):
        r = await post(w, anna, f"lim{i}")
        assert r.status == 429
        body = r.json()
        assert_matches(body, "Error")
        assert body["code"] == "rate_limited"
        ra = int(r.headers["retry-after"])
        assert 1 <= ra <= 86_400
        assert r.headers["cache-control"] == "no-store"
    assert len(w.agent.kinds("stream")) == calls_before
    texts = [t for a, t in await turns_of(engine, w, anna.uid) if a == "owner"]
    assert texts == ["ok0", "ok1", "lim0", "lim1", "lim2"]
    w.env.limit = 3
    assert (await post(w, anna, "ещё")).status == 200
    assert (await post(w, anna, "и ещё")).status == 429
    # Лимит — на пользователя: у другого свой счётчик.
    olga = await enter(w, "Ольга")
    assert (await post(w, olga, "привет")).status == 200


async def test_limit_race_at_boundary_memory(w: World) -> None:
    """Гонка у границы (атомарность, процесс один): 12 одновременных реплик при лимите 4 —
    ровно 4 × 200 и 8 × 429."""
    w.env.limit = 4
    anna = await enter(w, "Анна")
    rs = await asyncio.gather(*(post(w, anna, f"r{i}") for i in range(12)))
    assert sorted(r.status for r in rs) == [200] * 4 + [429] * 8


@pytest.fixture
def redis_url() -> Iterator[str]:
    """Свой redis-server во временном каталоге (не системный); нет бинарника — тест пропускается."""
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("redis-server не установлен")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    tmp = tempfile.mkdtemp(prefix="a8-redis-")
    proc = subprocess.Popen(  # noqa: S603 — фиксированная команда
        [
            binary,
            "--port",
            str(port),
            "--bind",
            "127.0.0.1",
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            tmp,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        proc.terminate()
        proc.wait(5)
        shutil.rmtree(tmp, ignore_errors=True)


async def test_limit_race_redis_many_clients(redis_url: str) -> None:
    """Атомарность для нескольких процессов (Lua в Redis): 6 независимых клиентов × 10
    одновременных попыток при лимите 7 — ровно 7 приняты; кризисная (`force`) сверх лимита —
    принята и засчитана; `Retry-After` ≥ 1; ключ живёт не дольше окна + час; в ключе нет
    ничего, кроме id и даты."""
    limiters = [RedisDailyLimit.from_url(redis_url) for _ in range(6)]
    uid = uuid.uuid4()
    try:
        res = await asyncio.gather(
            *(lim.acquire(uid, 7, force=False) for lim in limiters for _ in range(10))
        )
        assert sum(d.allowed for d in res) == 7
        assert all(d.retry_after_s >= 1 for d in res)
        forced = await limiters[0].acquire(uid, 7, force=True)
        assert forced.allowed and forced.count == 8
        denied = await limiters[1].acquire(uid, 7, force=False)
        assert not denied.allowed and denied.count == 8
        from redis.asyncio import Redis

        r = Redis.from_url(redis_url)
        keys = [k.decode() if isinstance(k, bytes) else k for k in await r.keys("*")]
        assert len(keys) == 1 and re.fullmatch(rf"feelings:chat:daily:{uid}:\d{{8}}", keys[0])
        ttl = await r.ttl(keys[0])
        assert 0 < ttl <= 86_400 + 3_600
        await r.aclose()
    finally:
        for lim in limiters:
            await lim.aclose()


class BrokenLimit:
    async def acquire(self, *a: Any, **k: Any) -> Any:
        raise ConnectionError("redis down")


async def test_limit_store_failure(w: World, caplog: pytest.LogCaptureFixture) -> None:
    """Сбой хранилища лимита: ответ не ломается (реализация — «пропустить», см. О-1 раунда),
    кризис — тоже; в журнале — только вид ошибки, без id пользователя."""
    caplog.set_level(logging.DEBUG)
    w.rt.limiter = BrokenLimit()
    anna = await enter(w, "Анна")
    assert stream_ok(await post(w, anna, "привет"))[-1][0] == "done"
    w.clf.verdict = CRISIS
    assert_crisis(stream_ok(await post(w, anna, "плохо")))
    assert "ConnectionError" in caplog.text
    assert str(anna.uid) not in caplog.text


# =============================================================== 5. Отключение клиента


def _live(before: set[asyncio.Task[Any]]) -> set[asyncio.Task[Any]]:
    cur = asyncio.current_task()
    return {t for t in asyncio.all_tasks() - before if not t.done() and t is not cur}


async def test_disconnect_during_prepare(w: World, engine: AsyncEngine) -> None:
    """АР-32 «отключение клиента»: клиент ушёл, пока работает классификатор — вызов отменён,
    агент не вызывался, задач не осталось; реплика владельца сохранена, ответа агента нет."""
    w.clf.verdict_delay_s = 5
    anna = await enter(w, "Анна")
    before = asyncio.all_tasks()
    gone = asyncio.Event()
    asyncio.get_running_loop().call_later(0.4, gone.set)
    r = await post(w, anna, "привет", disconnect=gone)
    assert r.status in (0, 499)
    await asyncio.sleep(0.05)
    assert _live(before) == set()
    assert w.clf.verdict_cancelled and w.agent.kinds("stream") == []
    assert await turns_of(engine, w, anna.uid) == [("owner", "привет")]


@pytest.mark.parametrize("after_gone", ["raise", "drop"])
@pytest.mark.parametrize("phase", ["first_delta", "keepalive_wait"])
async def test_disconnect_during_stream(
    phase: str, after_gone: str, w: World, engine: AsyncEngine
) -> None:
    """АР-32, правила этапа 2 (`aclosing`): клиент ушёл посреди ответа — поток агента отменён и
    закрыт, живых задач нет, недописанный ответ не сохранён, фонового сжатия нет."""
    w.agent.stream = [*_chunks(_answer(REPLY))[:8], *["x"] * 100]
    w.agent.chunk_delay_s = 0.1 if phase == "first_delta" else 0.0
    if phase == "keepalive_wait":
        w.agent.first_delay_s = 0.0
        w.agent.stream = [*_chunks(_answer(REPLY))[:8], LLMUnavailable("never")]
        w.agent.chunk_delay_s = 3.0
        w.env.keepalive_s = 0.05
    anna = await enter(w, "Анна")
    before = asyncio.all_tasks()
    gone = asyncio.Event()

    def on_chunk(resp: Resp) -> None:
        if "event: delta" in resp.body and not gone.is_set():
            asyncio.get_running_loop().call_later(0.15, gone.set)

    r = await post(w, anna, "привет", disconnect=gone, on_chunk=on_chunk, after_gone=after_gone)
    assert r.status == 200 and "event: delta" in r.body
    assert not any(f"event: {n}" in r.body for n in FINAL)
    await asyncio.sleep(0.05)
    assert _live(before) == set()
    assert w.agent.stream_cancelled and w.agent.stream_closed
    assert await turns_of(engine, w, anna.uid) == [("owner", "привет")]
    assert w.rt.background == set()


async def test_disconnect_before_response_start_in_limit_check(
    w: World, engine: AsyncEngine
) -> None:
    """Отключение после классификатора, до потока: ответ не стартует надолго, задач нет."""
    anna = await enter(w, "Анна")
    before = asyncio.all_tasks()
    gone = asyncio.Event()
    gone.set()
    r = await post(w, anna, "привет", disconnect=gone)
    await asyncio.sleep(0.05)
    assert r.status in (0, 200, 499)
    assert _live(before) == set()
    owner = [t for a, t in await turns_of(engine, w, anna.uid) if a == "owner"]
    assert owner == ["привет"]


# =============================================================== 6. Фоновое сжатие


class GateSummary(FakeModel):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = False

    async def complete_json(self, **kw: Any) -> StructuredResult:
        if "summary" in kw["schema"]["properties"]:
            self.started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return await super().complete_json(**kw)


async def test_background_compression_runs_and_stops(w: World, engine: AsyncEngine) -> None:
    """АР-32/код этапа 2: сжатие истории — фоновая задача после `done`, ответ не ждёт её; при
    остановке (`ChatRuntime.aclose`) снимается, вызов модели отменён, задач не остаётся."""
    gate = GateSummary()
    w.rt.models = models_of(w.agent, w.clf, gate)
    w.rt.compression = CompressionPolicy(trigger_turns=7, keep_turns=6)
    anna = await enter(w, "Анна")
    for i in range(4):
        r = await post(w, anna, f"m{i}")
        assert stream_ok(r)[-1][0] == "done"
    await asyncio.wait_for(gate.started.wait(), 5)
    assert len(w.rt.background) == 1
    await w.rt.aclose()
    assert gate.cancelled and w.rt.background == set()


async def test_background_compression_does_not_leak(
    w: World, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Фоновое сжатие не течёт: после серии ответов (в т.ч. со сбоем сжатия) задачи завершаются
    и удаляются из набора; ошибки не всплывают как «Task exception was never retrieved»."""
    caplog.set_level(logging.DEBUG)
    loop_errors: list[Any] = []
    loop = asyncio.get_running_loop()
    old = loop.get_exception_handler()
    loop.set_exception_handler(lambda _l, ctx: loop_errors.append(ctx))
    try:
        w.rt.compression = CompressionPolicy(trigger_turns=7, keep_turns=6)
        w.summ.summary = LLMUnavailable("down")
        anna = await enter(w, "Анна")
        for i in range(8):
            assert stream_ok(await post(w, anna, f"m{i}"))[-1][0] == "done"
        await w.rt.drain()
        assert w.rt.background == set()
        w.summ.summary = {"summary": "S"}
        assert stream_ok(await post(w, anna, "ещё"))[-1][0] == "done"
        await w.rt.drain()
        assert w.rt.background == set()
        assert await su(engine, "SELECT 1 FROM conversations c JOIN agents a ON a.id = c.agent_id "
                        "WHERE a.user_id = :u AND c.summary_enc IS NOT NULL", u=anna.uid)  # fmt: skip
    finally:
        loop.set_exception_handler(old)
    assert loop_errors == []


def test_runtime_rejects_small_keep_turns() -> None:
    """Правило этапа 2 (тест 2.1 О-5): `keep_turns` ≥ 6."""
    with pytest.raises(ValueError):
        ChatRuntime(compression=CompressionPolicy(trigger_turns=8, keep_turns=5))


# =============================================================== 7. История


async def test_history_order_cursor_and_no_store(w: World, engine: AsyncEngine) -> None:
    """Контракт `GET /chat/messages`: от новых к старым, страницы по курсору без повторов и
    пропусков; новые реплики после выдачи курсора страницы не сдвигают; `no-store`; схема."""
    anna = await enter(w, "Анна")
    for i in range(4):
        await post(w, anna, f"h{i}")
    all_rows = await turns_of(engine, w, anna.uid)
    assert len(all_rows) == 8
    r = await w.http.get(CHAT, headers=anna.h, params={"limit": 3})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    page = r.json()
    for item in page["items"]:
        assert_matches(item, "ChatMessage")
    seen = [i["text"] for i in page["items"]]
    cursor = page["next_cursor"]
    await post(w, anna, "новая после курсора")
    while cursor:
        r = await w.http.get(CHAT, headers=anna.h, params={"limit": 3, "cursor": cursor})
        assert r.status_code == 200
        page = r.json()
        seen += [i["text"] for i in page["items"]]
        cursor = page["next_cursor"]
    assert seen == [t for _, t in reversed(all_rows)]
    first = (await w.http.get(CHAT, headers=anna.h, params={"limit": 1})).json()
    assert first["items"][0]["text"] == REPLY and first["next_cursor"]


async def test_history_bad_cursors(w: World) -> None:
    """Контракт: курсор, который сервер не выдавал, — 422: подделка, порча подписи, чужой курсор
    (выданный другому пользователю), мусор; лимит вне 1..100 — 422; 401/403."""
    anna = await enter(w, "Анна")
    olga = await enter(w, "Ольга")
    for i in range(3):
        await post(w, anna, f"a{i}")
        await post(w, olga, f"o{i}")
    c_anna = (await w.http.get(CHAT, headers=anna.h, params={"limit": 1})).json()["next_cursor"]
    head, sig = c_anna.split(".")
    forged_head = chat_api._b64(f"{datetime.now(UTC).isoformat()}|{uuid.uuid4()}".encode())
    bad = [
        "x",
        "a.b.c",
        "",
        f"{forged_head}.{sig}",
        f"{head}.{sig[:-2]}AA",
        f"{head}.",
        "=" * 10,
        "!!.!!",
    ]
    for cur in bad:
        r = await w.http.get(CHAT, headers=anna.h, params={"cursor": cur})
        assert r.status_code == 422, cur
    r = await w.http.get(CHAT, headers=olga.h, params={"cursor": c_anna})
    assert r.status_code == 422
    for lim in (0, 101, -1):
        assert (await w.http.get(CHAT, headers=anna.h, params={"limit": lim})).status_code == 422
    assert (await w.http.get(CHAT)).status_code == 401
    raw = await enter(w, "Без", onboard=False)
    assert (await w.http.get(CHAT, headers=raw.h)).status_code == 403


async def test_history_cursor_after_account_delete(w: World) -> None:
    """АР-23: после удаления аккаунта и повторного входа тем же Telegram ID — новый пользователь:
    старый курсор не разбирается (422), история пуста, старый токен — 401."""
    anna = await enter(w, "Анна")
    for i in range(3):
        await post(w, anna, f"a{i}")
    cur = (await w.http.get(CHAT, headers=anna.h, params={"limit": 1})).json()["next_cursor"]
    r = await w.http.delete(f"{V1}/me", headers=anna.h)
    assert r.status_code in (200, 204), r.text
    assert (await w.http.get(CHAT, headers=anna.h)).status_code == 401
    again = await enter(w, "Анна", tg=anna.tg)
    assert again.uid != anna.uid
    assert (await w.http.get(CHAT, headers=again.h, params={"cursor": cur})).status_code == 422
    assert (await w.http.get(CHAT, headers=again.h)).json() == {"items": [], "next_cursor": None}


async def test_history_unavailable_text(w: World, engine: AsyncEngine) -> None:
    """Контракт: нерасшифровываемая реплика не пропускается и не роняет ответ — `UnavailableText`
    с кодом причины; в журнале нет id."""
    anna = await enter(w, "Анна")
    await post(w, anna, "привет")
    await su(
        engine,
        "UPDATE conversation_messages m SET text_enc = :b FROM conversations c, agents a "
        "WHERE c.id = m.conversation_id AND a.id = c.agent_id AND a.user_id = :u "
        "AND m.author = 'owner'",
        b=b"\x00garbage",
        u=anna.uid,
    )
    items = (await w.http.get(CHAT, headers=anna.h)).json()["items"]
    assert len(items) == 2
    assert items[1]["text"]["unavailable"] is True and items[1]["text"]["reason"]
    assert_matches(items[1], "ChatMessage")


# =============================================================== 8. Изоляция


async def test_isolation_partner_ex_outsider(w: World, engine: AsyncEngine) -> None:
    """ТЗ §2 п.4, §6, §9.2 п.1, критерий 1 (через API): история и контекст агента — только свои.
    Партнёр, бывший партнёр и посторонняя: их личный чат не виден владельцу ни в истории, ни во
    входе модели; владелец не виден им."""
    anna = await enter(w, "Анна")
    igor = await enter(w, "Игорь")
    pavel = await enter(w, "Павел")
    olga = await enter(w, "Ольга")
    k, x = uuid.uuid4(), uuid.uuid4()
    await su(engine, "INSERT INTO couples (id, status) VALUES (:k, 'active'), (:x, 'dissolved')",
             k=k, x=x)  # fmt: skip
    await su(
        engine,
        "INSERT INTO couple_members (couple_id, user_id, role, active) VALUES "
        "(:x, :a, 'initiator', false), (:x, :p, 'partner', false), "
        "(:k, :a, 'initiator', true), (:k, :i, 'partner', true)",
        k=k, x=x, a=anna.uid, p=pavel.uid, i=igor.uid,
    )  # fmt: skip
    marks = {p.label: f"ЛИЧНОЕ-{p.label}-{uuid.uuid4().hex}" for p in (igor, pavel, olga)}
    for p in (igor, pavel, olga):
        assert stream_ok(await post(w, p, marks[p.label]))[-1][0] == "done"
    w.agent.calls.clear()
    w.clf.calls.clear()
    own = f"СВОЁ-{uuid.uuid4().hex}"
    assert stream_ok(await post(w, anna, own))[-1][0] == "done"
    sent = w.agent.dump() + w.clf.dump()
    assert own in sent
    assert not [m for m in marks.values() if m in sent]
    hist = json.dumps((await w.http.get(CHAT, headers=anna.h)).json(), ensure_ascii=False)
    assert own in hist and not [m for m in marks.values() if m in hist]
    for p in (igor, pavel, olga):
        theirs = json.dumps((await w.http.get(CHAT, headers=p.h)).json(), ensure_ascii=False)
        assert own not in theirs and marks[p.label] in theirs


# =============================================================== 9. Контакты помощи


async def test_help_contacts_without_onboarding(w: World) -> None:
    """ФТ-17, ТЗ §9.3, §11 п.5, АР-33: `GET /help/contacts` — без онбординга (и без подписки),
    `no-store`, по схеме `HelpContact`, не меньше одной записи; тот же список, что в `crisis`."""
    raw = await enter(w, "Без", onboard=False)
    r = await w.http.get(f"{V1}/help/contacts", headers=raw.h)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body == help_contacts() and len(body) >= 1
    for item in body:
        assert_matches(item, "HelpContact")
    assert any(c["phone"] == "112" for c in body)


def test_help_contacts_marked_unverified_and_copy_safe() -> None:
    """АР-33: список помечен «не проверен оператором»; вызывающий не может испортить общий
    список; пустой список/пустые поля — ошибка настройки."""
    raw = json.loads(HELP_CONTACTS_PATH.read_text(encoding="utf-8"))
    assert raw["verified_by_operator"] is False and "не проверен" in raw["note"]
    assert HELP_CONTACTS.verified_by_operator is False
    copy = help_contacts()
    copy[0]["phone"] = "000"
    copy.append({"name": "x"})
    assert help_contacts()[0]["phone"] == "112" and len(help_contacts()) == len(
        HELP_CONTACTS.contacts
    )
    for bad in ('{"contacts": []}', '{"contacts": [{"name": "", "phone": "1", "description": "d"}]}',
                '{"contacts": [{"name": "n", "phone": "1"}]}', "[]",
                '{"contacts": [{"name": "n", "phone": "1", "description": "d", '
                '"round_the_clock": "yes"}]}'):  # fmt: skip
        with pytest.raises(ConfigError):
            parse_help_contacts(bad)


@pytest.mark.parametrize("env", ["production", "prod", "staging", "DEV", "dev ", ""])
def test_start_outside_dev_without_verified_list_refused(env: str) -> None:
    """АР-33: вне dev/test/local сервис не стартует без проверенного списка (в т.ч. при
    опечатке в имени окружения); в dev/test/local — стартует; проверенный список — стартует."""
    s = Env(env=env).settings()
    with pytest.raises(ConfigError):
        check_help_contacts_config(s)
    verified = parse_help_contacts(
        '{"verified_by_operator": true, "contacts": [{"name": "n", "phone": "1", '
        '"description": "d"}]}'
    )
    check_help_contacts_config(s, verified)
    for ok in ("dev", "test", "local"):
        check_help_contacts_config(Env(env=ok).settings())


async def test_lifespan_refuses_production_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """АР-33: запуск сервиса (lifespan) с `FEELINGS_ENV=production` и непроверенным списком —
    отказ до подключения к БД и Redis."""
    from fastapi import FastAPI

    from app.main import lifespan

    monkeypatch.setenv("FEELINGS_ENV", "production")
    monkeypatch.setenv("FEELINGS_TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("FEELINGS_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("FEELINGS_BOT_USERNAME", "A8_Chat_bot")
    monkeypatch.setenv("FEELINGS_TELEGRAM_WEBHOOK_SECRET", "a8-webhook-secret-" + "w" * 20)
    monkeypatch.setenv("FEELINGS_WEBAPP_URL", "https://a8.example.test/app")
    get_settings.cache_clear()
    probe = FastAPI()
    probe.state.sessionmaker = None
    try:
        with pytest.raises(ConfigError, match="help_contacts"):
            async with lifespan(probe):
                pass
    finally:
        get_settings.cache_clear()


# =============================================================== 10. Журналы


async def test_logs_have_no_texts_ids_tokens(
    w: World, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """CLAUDE.md «в журналах нет текстов», ТЗ §7, §9.1 п.5, контракт «Журналы»: весь путь
    (done, error, crisis, 429, сбой сборки, история, отключение) — в журнале ни текстов реплик и
    ответов, ни id (uuid), ни токенов."""
    caplog.set_level(logging.DEBUG)
    secret = f"СЕКРЕТ-{uuid.uuid4().hex}"
    reply_mark = f"ОТВЕТ-{uuid.uuid4().hex}"
    w.agent.stream = _chunks(_answer(reply_mark))
    w.env.limit = 3
    anna = await enter(w, "Анна")
    stream_ok(await post(w, anna, secret))
    w.rt.models = models_of(FakeModel(stream=[LLMUnavailable("x")]), w.clf, w.summ)
    stream_ok(await post(w, anna, secret + "-2"))
    w.clf.verdict = CRISIS
    stream_ok(await post(w, anna, secret + "-3"))
    w.clf.verdict = {"category": "none", "secondary": []}
    assert (await post(w, anna, secret + "-4")).status == 429
    await w.http.get(CHAT, headers=anna.h)
    await w.rt.drain()
    logs = caplog.text
    assert secret not in logs and reply_mark not in logs
    assert not UUID_RE.search(logs), UUID_RE.findall(logs)[:3]
    assert anna.token not in logs and str(anna.tg) not in logs
    assert "event=done" in logs and "event=rate_limited" in logs

"""3.1 (A2): черновики по HTTP — `POST /drafts`, `GET/PATCH /drafts/{id}`,
`POST /drafts/{id}/regenerate|reject|send`.

ФТ-07, ФТ-08, ФТ-17; ТЗ §2 п.2, §4.3, §6.2 п.1–2, §9.3; DECISIONS.md АР-11, АР-20, АР-24, АР-40;
Б-38. Критерий RoadMap: **без нажатия «Отправить» запись в `partner_messages` не создаётся** —
проверяется после каждого пути (создание, правка, «другой вариант», отказ, кризис, ошибки).
Модель — заглушка (`ScriptedProvider`), сеть не нужна; данные выдуманные."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.ai.llm import LLMRefusal, LLMUnavailable, StructuredResult
from app.ai.models import AgentModels
from app.ai.stub import ScriptedProvider
from app.api.chat import ChatRuntime
from app.config import Settings, get_settings
from app.core import crypto
from app.core.crypto import Keyring, generate_key_entry, parse_keyring
from app.help import help_contacts
from app.main import app
from app.repositories import chat
from tests.contract import assert_matches
from tests.test_auth_api import SECRET, TOKEN, _init_data, _tg
from tests.test_couple_api import BOT, LINK_RE

SM = async_sessionmaker[AsyncSession]
DRAFTS = "/api/v1/drafts"


def draft_answer(body: str) -> dict[str, Any]:
    return {
        "feeling": "Мне тревожно",
        "situation": "траты без обсуждения",
        "need": "предсказуемость",
        "request": "обсуждать крупные траты заранее",
        "text": body,
    }


class Model(ScriptedProvider):
    """``texts`` — черновики по очереди (последний повторяется); ``safety`` — категория второй
    линии; ``fail`` — исключение на вызове черновика."""

    def __init__(self, *texts_: str, safety: str = "none", fail: Exception | None = None) -> None:
        super().__init__(self._handle)
        self.texts = list(texts_) or [f"Мне тревожно. Давай обсуждать траты заранее? {_u()}"]
        self.safety = safety
        self.fail = fail
        self.calls: dict[str, list[dict[str, Any]]] = {}

    def _handle(self, role: str, payload: dict[str, Any], system: str) -> dict[str, Any]:
        self.calls.setdefault(role, []).append(payload)
        if role == "safety":
            return {"category": self.safety, "secondary": []}
        assert role == "draft", role
        body = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return draft_answer(body)

    async def complete_json(self, **kw: Any) -> StructuredResult:
        if self.fail is not None and "need" in kw["schema"]["properties"]:
            raise self.fail
        return await super().complete_json(**kw)


def _u() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class U:
    headers: dict[str, str]
    id: uuid.UUID


@dataclass
class Env:
    client: httpx.AsyncClient
    keys: Keyring
    engine: AsyncEngine
    sm: SM
    rt: ChatRuntime
    marks: list[str] = field(default_factory=list)

    def use(self, model: Any) -> Any:
        self.rt.models = AgentModels.single(model)
        return model

    def mark(self, what: str) -> str:
        label = f"метка-{what}-{uuid.uuid4().hex}"
        self.marks.append(label)
        return label


@pytest.fixture
async def env(sessionmaker: SM, engine: AsyncEngine) -> AsyncIterator[Env]:
    keys = parse_keyring(generate_key_entry("k1"), "k1")

    def settings() -> Settings:
        return Settings(
            telegram_bot_token=SecretStr(TOKEN),
            session_secret=SecretStr(SECRET),
            session_ttl_s=600,
            bot_username=BOT,
        )

    rt = ChatRuntime(models=AgentModels.single(Model()))
    prev = (app.state.sessionmaker, app.state.keyring, app.state.chat)
    app.state.sessionmaker, app.state.keyring, app.state.chat = sessionmaker, keys, rt
    app.dependency_overrides[get_settings] = settings
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield Env(c, keys, engine, sessionmaker, rt)
    await rt.drain()
    app.dependency_overrides.clear()
    app.state.sessionmaker, app.state.keyring, app.state.chat = prev


async def _login(env: Env) -> U:
    resp = await env.client.post("/api/v1/auth/telegram", json={"init_data": _init_data(_tg())})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return U({"Authorization": f"Bearer {body['access_token']}"}, uuid.UUID(body["me"]["id"]))


async def _person(env: Env, name: str, partner: str = "Партнёр") -> U:
    user = await _login(env)
    resp = await env.client.put(
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


async def _pair(env: Env, a: U, b: U) -> None:
    resp = await env.client.post("/api/v1/couple", headers=a.headers)
    assert resp.status_code == 201, resp.text
    match = LINK_RE.match(resp.json()["link"])
    assert match
    resp = await env.client.post(f"/api/v1/invites/{match.group(1)}/accept", headers=b.headers)
    assert resp.status_code == 200, resp.text


async def talk(env: Env, user: U, *owner: str) -> None:
    conv = await chat.current_conversation(env.sm, user.id)
    for body in owner:
        await chat.add_message(env.sm, env.keys, user.id, conv, "owner", body)
        await chat.add_message(env.sm, env.keys, user.id, conv, "agent", "Слышу тебя.")


async def couple(env: Env) -> tuple[U, U]:
    anna, igor = await _person(env, "Анна", "Игорь"), await _person(env, "Игорь", "Анна")
    await _pair(env, anna, igor)
    await talk(env, anna, "Игорь опять потратил много без обсуждения, мне тревожно.")
    return anna, igor


async def partner_messages(env: Env) -> int:
    async with env.engine.connect() as conn:
        n: int = (await conn.execute(text("SELECT count(*) FROM partner_messages"))).scalar_one()
    return n


async def safety_types(env: Env, user: U) -> list[str]:
    async with env.engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT type::text FROM safety_events WHERE user_id = :u ORDER BY created_at"),
            {"u": user.id},
        )
        return list(rows.scalars())


async def create(env: Env, user: U, **body: Any) -> httpx.Response:
    return await env.client.post(DRAFTS, headers=user.headers, json=body or None)


def ok_draft(resp: httpx.Response, status: int = 201) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    assert resp.headers["cache-control"] == "no-store"
    body: dict[str, Any] = resp.json()
    assert_matches(body, "Draft")
    return body


def _error(resp: httpx.Response, status: int, code: str) -> None:
    assert resp.status_code == status, resp.text
    assert_matches(resp.json(), "Error")
    assert resp.json()["code"] == code, resp.json()


# --------------------------------------------------------------------------- создание и чтение


async def test_create_get_and_no_partner_message_without_send(env: Env) -> None:
    anna, igor = await couple(env)
    before = await partner_messages(env)
    body = env.mark("draft")
    env.use(Model(body))
    d = ok_draft(await create(env, anna))
    assert (d["kind"], d["text"], d["version"], d["status"], d["reply_to_id"]) == (
        "draft",
        body,
        1,
        "draft",
        None,
    )
    assert set(d) == {"kind", "id", "text", "reply_to_id", "version", "status"}  # частей нет
    got = ok_draft(await env.client.get(f"{DRAFTS}/{d['id']}", headers=anna.headers), 200)
    assert got == d
    # Партнёр, посторонний и неверный id — одинаково 404.
    olga = await _person(env, "Ольга", "Пётр")
    for who in (igor, olga):
        _error(await env.client.get(f"{DRAFTS}/{d['id']}", headers=who.headers), 404, "not_found")
    for target in (uuid.uuid4(), "not-a-uuid"):
        _error(await env.client.get(f"{DRAFTS}/{target}", headers=anna.headers), 404, "not_found")
    assert await partner_messages(env) == before


async def test_create_needs_active_couple_and_turns(env: Env) -> None:
    model = env.use(Model())
    solo = await _person(env, "Соло")
    await talk(env, solo, "Хочу кое-что сказать партнёру.")
    _error(await create(env, solo), 409, "couple_not_active")
    assert model.calls == {}  # модель не вызывалась
    anna, igor = await _person(env, "Анна", "Игорь"), await _person(env, "Игорь", "Анна")
    await _pair(env, anna, igor)
    _error(await create(env, anna), 422, "no_turns")
    before = await partner_messages(env)
    await talk(env, anna, "Мне обидно.")
    _error(await create(env, anna, reply_to_id=str(uuid.uuid4())), 422, "bad_reply_target")
    _error(await create(env, anna, extra=1), 422, "validation_error")
    assert await partner_messages(env) == before


async def test_crisis_returns_help_and_no_draft(env: Env) -> None:
    """Кризис (§9.3): 200 `DraftCrisis` — протокол и контакты, черновика нет, событие записано."""
    anna, _ = await couple(env)
    model = env.use(Model(safety="self_harm"))
    before = await partner_messages(env)
    resp = await create(env, anna)
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    out = resp.json()
    assert_matches(out, "DraftCrisis")
    assert out["kind"] == "crisis" and out["draft_id"] is None
    assert out["contacts"] == help_contacts() and out["text"]
    assert "draft" not in model.calls
    async with env.engine.connect() as conn:
        n = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM message_drafts d JOIN agents a ON a.id = d.agent_id "
                    "WHERE a.user_id = :u"
                ),
                {"u": anna.id},
            )
        ).scalar_one()
    assert n == 0
    await env.rt.drain()
    assert await safety_types(env, anna) == ["self_harm"]
    assert await partner_messages(env) == before


@pytest.mark.parametrize(
    ("exc", "code"),
    [(LLMUnavailable("down"), "llm_unavailable"), (LLMRefusal("no"), "llm_refusal")],
)
async def test_model_errors_503_and_nothing_written(env: Env, exc: Exception, code: str) -> None:
    anna, _ = await couple(env)
    env.use(Model(fail=exc))
    before = await partner_messages(env)
    resp = await create(env, anna)
    _error(resp, 503, code)
    assert resp.json()["message"] == exc.user_message  # type: ignore[attr-defined]
    assert await partner_messages(env) == before


async def test_models_not_configured_503(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    anna, _ = await couple(env)
    env.rt.models = None
    _error(await create(env, anna), 503, "llm_unavailable")


async def test_quote_maps_to_draft_quote(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ai import drafts as ai_drafts

    anna, _ = await couple(env)

    async def quoted(*a: Any, **kw: Any) -> Any:
        raise ai_drafts.DraftError("quote")

    monkeypatch.setattr(ai_drafts, "generate_draft", quoted)
    _error(await create(env, anna), 422, "draft_quote")


# --------------------------------------------------------------------------- правка


async def test_patch_versions_and_conflicts(env: Env) -> None:
    anna, _ = await couple(env)
    before = await partner_messages(env)
    d = ok_draft(await create(env, anna))
    mine = env.mark("edit") + "  \n свои слова "
    e = ok_draft(
        await env.client.patch(
            f"{DRAFTS}/{d['id']}", headers=anna.headers, json={"text": mine, "expected_version": 1}
        ),
        200,
    )
    assert (e["text"], e["version"]) == (mine, 2)  # текст автора как есть
    _error(
        await env.client.patch(
            f"{DRAFTS}/{d['id']}", headers=anna.headers, json={"text": "x", "expected_version": 1}
        ),
        409,
        "version_conflict",
    )
    got = ok_draft(await env.client.get(f"{DRAFTS}/{d['id']}", headers=anna.headers), 200)
    assert (got["text"], got["version"]) == (mine, 2)
    for body in (
        {"text": "x"},
        {"expected_version": 2},
        {"text": "", "expected_version": 2},
        {"text": "   ", "expected_version": 2},
        {"text": "я" * 4001, "expected_version": 2},
        {"text": "x", "expected_version": 0},
        {"text": "x", "expected_version": "2"},
        {"text": "x", "expected_version": 2, "extra": 1},
    ):
        _error(
            await env.client.patch(f"{DRAFTS}/{d['id']}", headers=anna.headers, json=body),
            422,
            "validation_error",
        )
    assert await partner_messages(env) == before


# --------------------------------------------------------------------------- «другой вариант»


async def test_regenerate_new_version_wish_same_and_crisis(env: Env) -> None:
    anna, _ = await couple(env)
    before = await partner_messages(env)
    first, second = env.mark("v1"), env.mark("v2")
    model = env.use(Model(first, second))
    d = ok_draft(await create(env, anna))
    r = ok_draft(
        await env.client.post(
            f"{DRAFTS}/{d['id']}/regenerate", headers=anna.headers, json={"wish": "мягче"}
        ),
        200,
    )
    assert (r["id"], r["text"], r["version"]) == (d["id"], second, 2)
    assert model.calls["draft"][-1]["revision"] == {"previous": first, "wish": "мягче"}
    # Совпадает с прежним — 422 same_variant, версия не меняется.
    env.use(Model(second))
    _error(
        await env.client.post(f"{DRAFTS}/{d['id']}/regenerate", headers=anna.headers),
        422,
        "same_variant",
    )
    # Кризис при «другом варианте»: прежний черновик не меняется, draft_id — он.
    env.use(Model(safety="violence"))
    resp = await env.client.post(f"{DRAFTS}/{d['id']}/regenerate", headers=anna.headers)
    assert resp.status_code == 200
    out = resp.json()
    assert_matches(out, "DraftOutcome")
    assert (out["kind"], out["draft_id"]) == ("crisis", d["id"])
    got = ok_draft(await env.client.get(f"{DRAFTS}/{d['id']}", headers=anna.headers), 200)
    assert (got["text"], got["version"]) == (second, 2)
    _error(
        await env.client.post(f"{DRAFTS}/{d['id']}/regenerate", headers=anna.headers,
                              json={"wish": "я" * 501}),
        422,
        "validation_error",
    )  # fmt: skip
    _error(
        await env.client.post(f"{DRAFTS}/{uuid.uuid4()}/regenerate", headers=anna.headers),
        404,
        "not_found",
    )
    assert await partner_messages(env) == before


# --------------------------------------------------------------------------- отказ и отправка


async def test_reject_then_nothing_editable_and_nothing_sent(env: Env) -> None:
    anna, _ = await couple(env)
    before = await partner_messages(env)
    d = ok_draft(await create(env, anna))
    for _ in range(2):  # повтор — 204 без изменений
        resp = await env.client.post(f"{DRAFTS}/{d['id']}/reject", headers=anna.headers)
        assert resp.status_code == 204 and resp.content == b""
    got = ok_draft(await env.client.get(f"{DRAFTS}/{d['id']}", headers=anna.headers), 200)
    assert got["status"] == "rejected"
    _error(
        await env.client.patch(
            f"{DRAFTS}/{d['id']}", headers=anna.headers, json={"text": "x", "expected_version": 1}
        ),
        409,
        "not_editable",
    )
    _error(
        await env.client.post(f"{DRAFTS}/{d['id']}/regenerate", headers=anna.headers),
        409,
        "not_editable",
    )
    _error(
        await env.client.post(
            f"{DRAFTS}/{d['id']}/send", headers=anna.headers, json={"expected_version": 1}
        ),
        409,
        "not_editable",
    )
    assert await partner_messages(env) == before


async def test_send_creates_processing_message_and_draft_is_frozen(env: Env) -> None:
    """«Отправить» — 202 `PartnerMessage` `processing`: автор видит в ленте, получатель — нет;
    отправленный черновик неизменяем (Б-38); оригинал — текст отправленной версии."""
    anna, igor = await couple(env)
    before = await partner_messages(env)
    d = ok_draft(await create(env, anna))
    mine = env.mark("sent")
    ok_draft(
        await env.client.patch(
            f"{DRAFTS}/{d['id']}", headers=anna.headers, json={"text": mine, "expected_version": 1}
        ),
        200,
    )
    _error(
        await env.client.post(
            f"{DRAFTS}/{d['id']}/send", headers=anna.headers, json={"expected_version": 1}
        ),
        409,
        "version_conflict",
    )
    assert await partner_messages(env) == before
    resp = await env.client.post(
        f"{DRAFTS}/{d['id']}/send", headers=anna.headers, json={"expected_version": 2}
    )
    assert resp.status_code == 202, resp.text
    m = resp.json()
    assert_matches(m, "PartnerMessage")
    assert (m["direction"], m["text"], m["status"]) == ("outgoing", mine, "processing")
    # Б-84 (ревью р.1): у только что отправленного причины задержки нет — поля есть и пусты.
    assert (m["withheld_reason"], m["withheld_crisis"]) == (None, None)
    assert await partner_messages(env) == before + 1
    feed = (await env.client.get("/api/v1/couple/messages", headers=anna.headers)).json()["items"]
    assert [i["id"] for i in feed] == [m["id"]] and feed[0]["text"] == mine
    assert (await env.client.get("/api/v1/couple/messages", headers=igor.headers)).json()[
        "items"
    ] == []
    got = ok_draft(await env.client.get(f"{DRAFTS}/{d['id']}", headers=anna.headers), 200)
    assert got["status"] == "sent"
    for path, body in (
        (f"{DRAFTS}/{d['id']}/send", {"expected_version": 2}),
        (f"{DRAFTS}/{d['id']}/regenerate", None),
        (f"{DRAFTS}/{d['id']}/reject", None),
    ):
        _error(await env.client.post(path, headers=anna.headers, json=body), 409, "not_editable")
    _error(
        await env.client.patch(
            f"{DRAFTS}/{d['id']}", headers=anna.headers, json={"text": "x", "expected_version": 2}
        ),
        409,
        "not_editable",
    )
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT original_enc, draft_id FROM partner_messages WHERE id = :m"),
                {"m": m["id"]},
            )
        ).one()
    assert env.keys.open(crypto.MESSAGE_ORIGINAL, row.draft_id, row.original_enc) == mine
    assert await partner_messages(env) == before + 1


async def test_send_after_partner_left_is_couple_not_active(env: Env) -> None:
    anna, igor = await couple(env)
    d = ok_draft(await create(env, anna))
    assert (await env.client.post("/api/v1/couple/leave", headers=igor.headers)).status_code == 204
    before = await partner_messages(env)
    _error(
        await env.client.post(
            f"{DRAFTS}/{d['id']}/send", headers=anna.headers, json={"expected_version": 1}
        ),
        409,
        "couple_not_active",
    )
    assert await partner_messages(env) == before
    got = ok_draft(await env.client.get(f"{DRAFTS}/{d['id']}", headers=anna.headers), 200)
    assert got["status"] == "draft"  # ничего не изменено


async def test_send_blocked_draft_is_422(env: Env) -> None:
    anna, _ = await couple(env)
    d = ok_draft(await create(env, anna))
    async with env.engine.begin() as conn:
        await conn.execute(
            text("UPDATE message_drafts SET status = 'blocked' WHERE id = :d"), {"d": d["id"]}
        )
    _error(
        await env.client.post(
            f"{DRAFTS}/{d['id']}/send", headers=anna.headers, json={"expected_version": 1}
        ),
        422,
        "blocked_by_safety",
    )


async def test_send_undecryptable_draft_refused_before_db_function(env: Env) -> None:
    """3.1 р1 (ревью MINOR-2): нерасшифрованный черновик не отправляется — 409 `not_editable` до
    `app_send_draft`, в `partner_messages` ничего; черновик остаётся `draft`; GET — 404 (Б-79)."""
    anna, _ = await couple(env)
    d = ok_draft(await create(env, anna))
    async with env.engine.begin() as conn:
        await conn.execute(
            text("UPDATE message_drafts SET text_enc = :b WHERE id = :d"),
            {"b": b"\x00broken", "d": d["id"]},
        )
    before = await partner_messages(env)
    _error(
        await env.client.post(
            f"{DRAFTS}/{d['id']}/send", headers=anna.headers, json={"expected_version": 1}
        ),
        409,
        "not_editable",
    )
    assert await partner_messages(env) == before
    async with env.engine.connect() as conn:
        status = (
            await conn.execute(
                text("SELECT status::text FROM message_drafts WHERE id = :d"), {"d": d["id"]}
            )
        ).scalar_one()
    assert status == "draft"
    _error(await env.client.get(f"{DRAFTS}/{d['id']}", headers=anna.headers), 404, "not_found")


async def test_long_model_draft_retried_shorter_then_clipped(env: Env) -> None:
    """3.1 р1 (ревью MINOR-3): черновик модели длиннее 4000 (предел PATCH) — повтор с пожеланием
    «короче»; снова длинный — обрезка по границе предложения; текст можно сохранить правкой."""
    from app.ai.drafts import MAX_DRAFT_CHARS, SHORTER_WISH

    anna, _ = await couple(env)
    short = env.mark("short")
    model = env.use(Model("Очень длинно. " * 400, short))
    d = ok_draft(await create(env, anna))
    assert d["text"] == short
    assert model.calls["draft"][-1]["revision"]["wish"] == SHORTER_WISH
    long_ = "Мне важно, чтобы мы обсуждали траты заранее. " * 120
    env.use(Model(long_))
    d2 = ok_draft(await create(env, anna))
    assert len(d2["text"]) <= MAX_DRAFT_CHARS and d2["text"].endswith(".")
    assert long_.startswith(d2["text"])
    # «Другой вариант» — тот же предел.
    env.use(Model(long_ + "!"))
    r = ok_draft(await env.client.post(f"{DRAFTS}/{d['id']}/regenerate", headers=anna.headers), 200)
    assert len(r["text"]) <= MAX_DRAFT_CHARS
    e = await env.client.patch(
        f"{DRAFTS}/{d2['id']}",
        headers=anna.headers,
        json={"text": d2["text"], "expected_version": 1},
    )
    assert e.status_code == 200, e.text


def test_clip_draft_boundaries() -> None:
    from app.ai.drafts import clip_draft

    assert clip_draft("Коротко.", 100) == "Коротко."
    assert clip_draft("Раз. Два. Три четыре пять", 12) == "Раз. Два."
    assert clip_draft("словословослово " * 3, 20) == "словословослово"


async def test_race_send_and_patch_same_version(env: Env) -> None:
    """Одновременные «Отправить» и правка одной версии: проходит ровно одно; ушло — именно то,
    что автор видел (оригинал = текст этой версии), правка не теряется молча."""
    anna, _ = await couple(env)
    for _ in range(5):
        d = ok_draft(await create(env, anna))
        before = await partner_messages(env)
        send, patch = await asyncio.gather(
            env.client.post(
                f"{DRAFTS}/{d['id']}/send", headers=anna.headers, json={"expected_version": 1}
            ),
            env.client.patch(
                f"{DRAFTS}/{d['id']}",
                headers=anna.headers,
                json={"text": "правка", "expected_version": 1},
            ),
        )
        statuses = sorted((send.status_code, patch.status_code))
        assert statuses in ([200, 409], [202, 409]), (send.text, patch.text)
        if send.status_code == 202:
            assert patch.json()["code"] == "not_editable"
            assert send.json()["text"] == d["text"]
            assert await partner_messages(env) == before + 1
        else:
            assert send.json()["code"] == "version_conflict"
            assert await partner_messages(env) == before


# --------------------------------------------------------------------------- доступ и журналы


async def test_access_401_403_everywhere(env: Env) -> None:
    fresh = await _login(env)
    target = uuid.uuid4()
    ops = (
        ("POST", DRAFTS, None),
        ("GET", f"{DRAFTS}/{target}", None),
        ("PATCH", f"{DRAFTS}/{target}", {"text": "x", "expected_version": 1}),
        ("POST", f"{DRAFTS}/{target}/regenerate", None),
        ("POST", f"{DRAFTS}/{target}/reject", None),
        ("POST", f"{DRAFTS}/{target}/send", {"expected_version": 1}),
    )
    for method, path, body in ops:
        for headers in ({}, {"Authorization": "Bearer nope"}):
            _error(
                await env.client.request(method, path, headers=headers, json=body),
                401,
                "unauthorized",
            )
        _error(
            await env.client.request(method, path, headers=fresh.headers, json=body),
            403,
            "onboarding_required",
        )


async def test_logs_have_no_texts(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    anna, _ = await couple(env)
    first = env.mark("log1")
    env.use(Model(first, env.mark("log2")))
    d = ok_draft(await create(env, anna))
    await env.client.post(f"{DRAFTS}/{d['id']}/regenerate", headers=anna.headers,
                          json={"wish": env.mark("wish")})  # fmt: skip
    mine = env.mark("log3")
    e = await env.client.patch(
        f"{DRAFTS}/{d['id']}", headers=anna.headers, json={"text": mine, "expected_version": 2}
    )
    assert e.status_code == 200
    await env.client.post(f"{DRAFTS}/{d['id']}/send", headers=anna.headers,
                          json={"expected_version": 3})  # fmt: skip
    env.use(Model(safety="self_harm"))
    await create(env, anna)
    logged = "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)
    for label in env.marks:
        assert label not in logged and label.split("-")[-1] not in logged
    assert "drafts: event=created" in logged and "drafts: event=crisis" in logged
    assert str(anna.id) not in logged

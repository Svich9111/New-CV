"""3.1 и 3.3, A8: независимая проверка черновиков (`/drafts*`) и ленты пары (`/couple/messages*`)
(ФТ-07…ФТ-11; ТЗ §2 п.2–3, §4.3–4.4, §6.2 п.1–2, п.7, §7, §9.3; АР-11, АР-25, АР-40; Б-38, Б-77).

Вход — initData, онбординг — по HTTP; пары и сообщения конвейера — суперпользователем и
`texts.pipeline_deliver` под `feelings_worker` (конвейер 3.2 ещё не сделан). Модель — своя заглушка
(`DModel`), сеть не используется. Хелперы исполнителей (`test_drafts_*`, `test_couple_messages_api`)
не используются. Данные выдуманные. Дефекты — `xfail(strict=True)` (docs/reviews/3.1-3.3-test.md)."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.ai.knowledge import KnowledgeFragment
from app.ai.llm import ChatMessage, LLMRefusal, LLMUnavailable, StructuredResult, Usage
from app.ai.models import AgentModels
from app.api.chat import ChatRuntime
from app.config import Settings, get_settings
from app.core.telegram import sign_init_data
from app.db import user_scope
from app.help import help_contacts
from app.main import app
from app.repositories import chat as chat_repo
from app.repositories import couple_messages as cm_repo
from app.repositories import texts
from tests.contract import assert_matches
from tests.test_chat_a8 import KEYS, _pmsg
from tests.test_chat_api_a8 import UUID_RE, call

SM = async_sessionmaker[AsyncSession]
V1 = "/api/v1"
BOT_TOKEN = "777007:A8-drafts-bot-token-not-real"
SESSION_SECRET = "a8-drafts-session-secret-" + "d" * 24
FRAGMENT = (
    "Когда партнёр говорит о своей обиде, попробуйте сначала повторить его слова своими "
    "словами и только потом объяснять, почему вы поступили именно так, а не иначе."
)


# =============================================================== заглушка модели


def _draft(text_: str) -> dict[str, Any]:
    return {"feeling": "Мне грустно", "situation": "планы отменяются", "need": "время вместе",
            "request": "предупреждай заранее", "text": text_}  # fmt: skip


@dataclass
class DModel:
    """Роль — по полям схемы: безопасность (`category`), черновик (`need`), диалог (`draft_ready`)."""

    verdict: Any = field(default_factory=lambda: {"category": "none", "secondary": []})
    drafts: list[Any] = field(default_factory=list)  # по порядку вызовов черновика
    gate: asyncio.Event | None = None
    model: str = "d-stub"
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def complete_json(self, *, system: str, messages: Sequence[ChatMessage],
                            schema: Mapping[str, Any], max_tokens: int = 16000) -> StructuredResult:  # fmt: skip
        props = set(schema.get("properties", {}))
        payload = json.loads(messages[-1].content)
        role = "safety" if "category" in props else "draft" if "need" in props else "other"
        self.calls.append({"role": role, "payload": payload, "raw": messages[-1].content})
        if role == "safety":
            data = self.verdict
        else:
            if self.gate is not None:
                await self.gate.wait()
            data = self.drafts.pop(0) if self.drafts else _draft(f"Черновик {uuid.uuid4().hex[:6]}")
        if isinstance(data, BaseException):
            raise data
        return StructuredResult(data=data, model=self.model, usage=Usage(), latency_s=0, cost_usd=0)

    async def stream_text(self, **kw: Any) -> Any:  # pragma: no cover
        raise NotImplementedError
        yield ""


class FixedKnowledge:
    def __init__(self, frags: Sequence[str]) -> None:
        self.frags = list(frags)

    async def search(self, query: str, *, vector: Any, limit: int = 5) -> list[KnowledgeFragment]:
        return [KnowledgeFragment(f"k{i}", t) for i, t in enumerate(self.frags)][:limit]


# =============================================================== мир


@dataclass
class Person:
    uid: uuid.UUID
    token: str
    agent: uuid.UUID = field(default_factory=lambda: uuid.UUID(int=0))

    @property
    def h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@dataclass
class W:
    http: httpx.AsyncClient
    engine: AsyncEngine
    sm: SM
    model: DModel
    rt: ChatRuntime
    anna: Person
    igor: Person
    pavel: Person
    couple: uuid.UUID
    ex_couple: uuid.UUID


async def _enter(http: httpx.AsyncClient, label: str) -> Person:
    tg = 800_000_000_000 + secrets.randbelow(99_999_999_999)
    init = sign_init_data({"auth_date": str(int(time.time())),
                           "user": json.dumps({"id": tg, "first_name": label})}, BOT_TOKEN)  # fmt: skip
    r = await http.post(f"{V1}/auth/telegram", json={"init_data": init})
    assert r.status_code == 200, r.text
    p = Person(uuid.UUID(r.json()["me"]["id"]), r.json()["access_token"])
    r = await http.put(f"{V1}/me/onboarding", headers=p.h, json={
        "name": label, "address_form": "на ты", "partner_name": "Партнёр", "timezone": "Europe/Moscow",
        "consent_terms": True, "consent_data": True, "consent_sensitive": True, "age_confirmed": True,
    })  # fmt: skip
    assert r.status_code == 200, r.text
    return p


async def su(engine: AsyncEngine, sql: str, **params: object) -> Any:
    async with engine.begin() as c:
        res = await c.execute(text(sql), params)
        return res.all() if res.returns_rows else None


@pytest.fixture
async def w(engine: AsyncEngine, sessionmaker: SM) -> AsyncIterator[W]:
    def settings() -> Settings:
        return Settings(telegram_bot_token=SecretStr(BOT_TOKEN),
                        session_secret=SecretStr(SESSION_SECRET), session_ttl_s=900,
                        bot_username="A8_Drafts_bot", env="test")  # fmt: skip

    m = DModel()
    rt = ChatRuntime(models=AgentModels(m, m, m, m, m, m, summary=m))
    saved = (app.state.sessionmaker, app.state.keyring, app.state.chat)
    app.state.sessionmaker, app.state.keyring, app.state.chat = sessionmaker, KEYS, rt
    app.dependency_overrides[get_settings] = settings
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                 base_url="http://a8") as http:  # fmt: skip
        anna = await _enter(http, "Анна")
        igor = await _enter(http, "Игорь")
        pavel = await _enter(http, "Павел")
        for p in (anna, igor, pavel):
            p.agent = (await su(engine, "SELECT id FROM agents WHERE user_id = :u", u=p.uid))[0][0]
        k, x = uuid.uuid4(), uuid.uuid4()
        await su(engine, "INSERT INTO couples (id, status) VALUES (:k, 'active'), (:x, 'dissolved')",
                 k=k, x=x)  # fmt: skip
        await su(engine, "INSERT INTO couple_members (couple_id, user_id, role, active) VALUES "
                 "(:x, :a, 'initiator', false), (:x, :p, 'partner', false), "
                 "(:k, :a, 'initiator', true), (:k, :i, 'partner', true)",
                 k=k, x=x, a=anna.uid, p=pavel.uid, i=igor.uid)  # fmt: skip
        await chat_repo.add_owner_turn(
            sessionmaker, KEYS, anna.uid, "Меня обижает, что он опаздывает"
        )
        await chat_repo.add_owner_turn(sessionmaker, KEYS, igor.uid, "Мне тяжело с её упрёками")
        yield W(http, engine, sessionmaker, m, rt, anna, igor, pavel, k, x)
    await rt.aclose()
    app.dependency_overrides.clear()
    app.state.sessionmaker, app.state.keyring, app.state.chat = saved


async def pm_count(w: W) -> int:
    return int((await su(w.engine, "SELECT count(*) FROM partner_messages WHERE couple_id = :k",
                         k=w.couple))[0][0])  # fmt: skip


async def create(w: W, p: Person, **body: Any) -> httpx.Response:
    return await w.http.post(f"{V1}/drafts", headers=p.h, json=body or None)


async def send(w: W, p: Person, d: str, v: int) -> httpx.Response:
    return await w.http.post(f"{V1}/drafts/{d}/send", headers=p.h, json={"expected_version": v})


async def deliver(w: W, msg: uuid.UUID, version_text: str, *, adapted: bool) -> None:
    async with async_sessionmaker(w.engine)() as s, s.begin():
        await s.execute(text("SET LOCAL ROLE feelings_worker"))
        assert await texts.pipeline_deliver(s, KEYS, msg, version_text, is_adapted=adapted,
                                            tone=2 if adapted else 0, harsh=False)  # fmt: skip


# =============================================================== 3.1: без «Отправить» — ничего


async def test_no_path_without_send_creates_partner_message(w: W) -> None:
    """Кр.1 3.1 (RoadMap), ТЗ §2 п.2: создание, чтение, правка, «другой вариант», отказ,
    кризис при создании и при «другом варианте», ошибки модели (недоступность, отказ,
    испорченный ответ), цитата, неверный `reply_to_id` — ни одной строки `partner_messages`."""
    a = w.anna
    r = await create(w, a)
    assert r.status_code == 201, r.text
    assert_matches(r.json(), "Draft")
    d = r.json()
    assert (await w.http.get(f"{V1}/drafts/{d['id']}", headers=a.h)).status_code == 200
    r = await w.http.patch(f"{V1}/drafts/{d['id']}", headers=a.h,
                           json={"text": "Правка автора", "expected_version": 1})  # fmt: skip
    assert r.status_code == 200 and r.json()["version"] == 2
    r = await w.http.post(f"{V1}/drafts/{d['id']}/regenerate", headers=a.h, json={"wish": "мягче"})
    assert r.status_code == 200 and r.json()["kind"] == "draft"
    for err, code in ((LLMUnavailable("x"), "llm_unavailable"), (LLMRefusal("x"), "llm_refusal"),
                      ({"bad": 1}, "llm_unavailable")):  # fmt: skip
        w.model.drafts = [err, err]
        r = await create(w, a)
        assert r.status_code == 503 and r.json()["code"] == code, r.text
    w.model.drafts = []
    w.model.verdict = {"category": "self_harm", "secondary": []}
    assert (await create(w, a)).json()["kind"] == "crisis"
    assert (await w.http.post(f"{V1}/drafts/{d['id']}/regenerate", headers=a.h)).json()[
        "kind"
    ] == "crisis"
    w.model.verdict = {"category": "none", "secondary": []}
    w.rt.knowledge = FixedKnowledge([FRAGMENT])
    w.model.drafts = [_draft(FRAGMENT), _draft(FRAGMENT)]
    assert (await create(w, a)).json()["code"] == "draft_quote"
    w.rt.knowledge = None
    assert (await create(w, a, reply_to_id=str(uuid.uuid4()))).json()["code"] == "bad_reply_target"
    assert (await w.http.post(f"{V1}/drafts/{d['id']}/reject", headers=a.h)).status_code == 204
    assert await pm_count(w) == 0


async def test_client_disconnect_during_generation(w: W) -> None:
    """Клиент ушёл, пока модель пишет черновик: в `partner_messages` ничего (черновик может
    остаться — он не отправлен)."""
    w.model.gate = asyncio.Event()
    gone = asyncio.Event()
    task = asyncio.create_task(call("POST", f"{V1}/drafts", headers=w.anna.h, body={},
                                    disconnect=gone))  # fmt: skip
    await asyncio.sleep(0.3)
    gone.set()
    w.model.gate.set()
    await task
    assert await pm_count(w) == 0


# =============================================================== 3.1: отправка


async def test_send_requires_seen_version_and_only_once(w: W) -> None:
    """ФТ-08, АР-40: «Отправить» — только с увиденной версией (иначе 409 `version_conflict`),
    один раз (повтор — 409 `not_editable`); 202 `processing`; оригинал = текст этой версии;
    отправленный неизменяем (PATCH, «другой вариант», отказ — 409; прямой UPDATE — 0 строк)."""
    a = w.anna
    d = (await create(w, a)).json()
    await w.http.patch(f"{V1}/drafts/{d['id']}", headers=a.h,
                       json={"text": "ТЕКСТ-V2-a8", "expected_version": 1})  # fmt: skip
    r = await send(w, a, d["id"], 1)
    assert r.status_code == 409 and r.json()["code"] == "version_conflict"
    assert await pm_count(w) == 0
    r = await send(w, a, d["id"], 2)
    assert r.status_code == 202 and r.json()["status"] == "processing"
    assert_matches(r.json(), "PartnerMessage")
    assert r.json()["text"] == "ТЕКСТ-V2-a8"
    for again in (await send(w, a, d["id"], 2), await send(w, a, d["id"], 3)):
        assert again.status_code == 409
    for resp in (
        await w.http.patch(
            f"{V1}/drafts/{d['id']}", headers=a.h, json={"text": "x", "expected_version": 2}
        ),
        await w.http.post(f"{V1}/drafts/{d['id']}/regenerate", headers=a.h),
        await w.http.post(f"{V1}/drafts/{d['id']}/reject", headers=a.h),
    ):
        assert resp.status_code == 409 and resp.json()["code"] == "not_editable", resp.text
    async with user_scope(w.sm, a.uid) as s:
        res = await s.execute(text("UPDATE message_drafts SET text_enc = 'x' WHERE id = :d "
                                   "RETURNING id"), {"d": d["id"]})  # fmt: skip
        assert res.all() == []
    assert await pm_count(w) == 1
    # Партнёр и посторонний черновик не видят.
    for p in (w.igor, w.pavel):
        assert (await w.http.get(f"{V1}/drafts/{d['id']}", headers=p.h)).status_code == 404
        assert (await send(w, p, d["id"], 2)).status_code == 404


async def test_send_races(w: W) -> None:
    """ФТ-08, АР-13/24: гонки «Отправить» — send×send (одна 202), send×PATCH (одна сторона
    побеждает, текст сообщения — именно отправленной версии), send×«другой вариант» (вариант не
    перезаписывает отправленный) — сообщение ровно одно, без 500."""
    a = w.anna
    for _ in range(4):
        d = (await create(w, a)).json()
        pair = await asyncio.gather(send(w, a, d["id"], 1), send(w, a, d["id"], 1))
        assert sorted(r.status_code for r in pair) == [202, 409]
    assert await pm_count(w) == 4
    for _ in range(4):
        d = (await create(w, a)).json()
        rs, rp = await asyncio.gather(
            send(w, a, d["id"], 1),
            w.http.patch(
                f"{V1}/drafts/{d['id']}",
                headers=a.h,
                json={"text": "ПРАВКА-a8", "expected_version": 1},
            ),
        )
        assert {rs.status_code, rp.status_code} in ({202, 409}, {200, 409}), (rs.text, rp.text)
        if rs.status_code == 202:
            assert rs.json()["text"] == d["text"]
    d = (await create(w, a)).json()
    w.model.gate = asyncio.Event()
    regen = asyncio.create_task(w.http.post(f"{V1}/drafts/{d['id']}/regenerate", headers=a.h))
    await asyncio.sleep(0.3)
    sent = await send(w, a, d["id"], 1)
    w.model.gate.set()
    rr = await regen
    assert sent.status_code == 202 and sent.json()["text"] == d["text"]
    assert rr.status_code == 409 and rr.json()["code"] == "not_editable"
    w.model.gate = None


@pytest.mark.parametrize("other", ["leave", "partner_deletes_account"])
async def test_send_vs_couple_end(other: str, w: W) -> None:
    """send × выход из пары / удаление аккаунта партнёра одновременно: 202 (успел до) или 409
    `couple_not_active`; без 500 и 40P01; после — новых сообщений нет."""
    a = w.anna
    drafts = [(await create(w, a)).json() for _ in range(3)]

    async def end() -> httpx.Response:
        if other == "leave":
            return await w.http.post(f"{V1}/couple/leave", headers=w.igor.h)
        return await w.http.delete(f"{V1}/me", headers=w.igor.h)

    out = await asyncio.gather(*(send(w, a, d["id"], 1) for d in drafts), end())
    codes = [r.status_code for r in out[:-1]]
    assert out[-1].status_code in (200, 204), out[-1].text
    assert all(c in (202, 409) for c in codes), [r.text for r in out]
    assert all(r.json()["code"] == "couple_not_active" for r in out[:-1] if r.status_code == 409)
    late = await create(w, a)
    assert late.status_code == 409 and late.json()["code"] == "couple_not_active"


# =============================================================== 3.1: кризис, цитата, ответ


async def test_crisis_draft(w: W) -> None:
    """§9.3, АР-40: кризис при создании — 200 `DraftCrisis` (контакты = `/help/contacts`,
    `draft_id` null), при «другом варианте» — прежний черновик не изменён (`draft_id` — его id);
    событие в `safety_events` (только тип); черновик при создании не записан."""
    a = w.anna
    d = (await create(w, a)).json()
    before = await su(
        w.engine, "SELECT count(*) FROM message_drafts WHERE agent_id = :g", g=a.agent
    )
    w.model.verdict = {"category": "violence", "secondary": []}
    r = await create(w, a)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert_matches(r.json(), "DraftCrisis")
    assert r.json()["contacts"] == help_contacts() and r.json()["draft_id"] is None
    r2 = await w.http.post(f"{V1}/drafts/{d['id']}/regenerate", headers=a.h)
    assert r2.json()["kind"] == "crisis" and r2.json()["draft_id"] == d["id"]
    got = (await w.http.get(f"{V1}/drafts/{d['id']}", headers=a.h)).json()
    assert (got["text"], got["version"]) == (d["text"], 1)
    after = await su(w.engine, "SELECT count(*) FROM message_drafts WHERE agent_id = :g", g=a.agent)
    assert before == after
    await w.rt.drain()
    ev = await su(w.engine, "SELECT type::text FROM safety_events WHERE user_id = :u", u=a.uid)
    assert [e[0] for e in ev] == ["violence", "violence"]


async def test_reply_to_only_delivered_current_couple(w: W) -> None:
    """ФТ-11, АР-40: `reply_to_id` — только доставленное владельцу сообщение текущей пары; своё,
    недоставленное, остановленное, бывшей пары, чужое — 422 `bad_reply_target`; ответ отправлен
    и доставлен — исходное «отвечено»."""
    async with w.engine.begin() as c:
        IG, AN, PV = (
            (w.igor.uid, w.igor.agent),
            (w.anna.uid, w.anna.agent),
            (w.pavel.uid, w.pavel.agent),
        )
        ok = await _pmsg(c, w.couple, IG, w.anna.uid, "ОТИГОРЯa8", "delivered", datetime.now(UTC),
                         for_recipient="ОТИГОРЯa8")  # fmt: skip
        own = await _pmsg(c, w.couple, AN, w.igor.uid, "СВОЁa8", "delivered", datetime.now(UTC),
                          for_recipient="x")  # fmt: skip
        proc = await _pmsg(c, w.couple, IG, w.anna.uid, "ЖДЁТa8", "processing", datetime.now(UTC))
        held = await _pmsg(c, w.couple, IG, w.anna.uid, "СТОПa8", "withheld", datetime.now(UTC))
        ex = await _pmsg(c, w.ex_couple, PV, w.anna.uid, "БЫВШИЙa8", "delivered", datetime.now(UTC),
                         for_recipient="x")  # fmt: skip
    for bad in (own, proc, held, ex):
        r = await create(w, w.anna, reply_to_id=str(bad))
        assert r.status_code == 422 and r.json()["code"] == "bad_reply_target", bad
    r = await create(w, w.anna, reply_to_id=str(ok))
    assert r.status_code == 201 and r.json()["reply_to_id"] == str(ok)
    sent = await send(w, w.anna, r.json()["id"], 1)
    assert sent.status_code == 202 and sent.json()["reply_to_id"] == str(ok)
    await deliver(w, uuid.UUID(sent.json()["id"]), "ответ Анны", adapted=False)
    feed = (await w.http.get(f"{V1}/couple/messages", headers=w.anna.h)).json()["items"]
    assert next(i for i in feed if i["id"] == str(ok))["status"] == "answered"
    # «Прочитано» после ответа — без изменений: `answered` не откатывается в `read`.
    rd = await w.http.post(f"{V1}/couple/messages/{ok}/read", headers=w.anna.h)
    assert rd.status_code in (200, 204), rd.text
    st = await su(w.engine, "SELECT status::text FROM partner_messages WHERE id = :m", m=ok)
    assert st[0][0] == "answered"


# =============================================================== 3.3: лента


async def _sent(w: W, p: Person, body: str) -> uuid.UUID:
    d = (await create(w, p)).json()
    await w.http.patch(
        f"{V1}/drafts/{d['id']}", headers=p.h, json={"text": body, "expected_version": 1}
    )
    r = await send(w, p, d["id"], 2)
    assert r.status_code == 202
    return uuid.UUID(r.json()["id"])


async def feed(w: W, p: Person, **params: Any) -> dict[str, Any]:
    r = await w.http.get(f"{V1}/couple/messages", headers=p.h, params=params)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body: dict[str, Any] = r.json()
    for it in body["items"]:
        assert_matches(it, "PartnerMessage")
    return body


async def test_feed_roles_statuses_and_original(w: W, caplog: pytest.LogCaptureFixture) -> None:
    """3.3 кр.1–2, ТЗ §2 п.3, §7, АР-25/40: `processing` — у автора виден, у получателя нет;
    после доставки адаптированной версии получатель видит её (`is_adapted`), автор — свой
    оригинал (версия для получателя ему не показывается); `withheld` получателю не виден;
    «прочитано» — получатель (повтор — без изменений), автору — 404; оригинал получателю —
    с фиксацией просмотра (повтор не сдвигает отметку), автору — без фиксации; журналы без
    текстов и id."""
    caplog.set_level(logging.DEBUG)
    m1 = await _sent(w, w.anna, "ОРИГИНАЛ-АННЫ-a8")
    held = await _sent(w, w.anna, "ОСТАНОВЛЕНО-a8")
    assert [i["id"] for i in (await feed(w, w.igor))["items"]] == []
    mine = {i["id"]: i for i in (await feed(w, w.anna))["items"]}
    assert mine[str(m1)]["status"] == "processing" and mine[str(m1)]["direction"] == "outgoing"
    await deliver(w, m1, "АДАПТИРОВАНО-ДЛЯ-ИГОРЯ-a8", adapted=True)
    async with async_sessionmaker(w.engine)() as s, s.begin():
        await s.execute(text("SET LOCAL ROLE feelings_worker"))
        await s.execute(text("SELECT pipeline_withhold(:m)"), {"m": held})
    ig = (await feed(w, w.igor))["items"]
    assert [(i["id"], i["text"], i["is_adapted"], i["direction"]) for i in ig] == [
        (str(m1), "АДАПТИРОВАНО-ДЛЯ-ИГОРЯ-a8", True, "incoming")]  # fmt: skip
    an = {i["id"]: i for i in (await feed(w, w.anna))["items"]}
    assert an[str(m1)]["text"] == "ОРИГИНАЛ-АННЫ-a8" and an[str(held)]["status"] == "withheld"
    assert "АДАПТИРОВАНО" not in json.dumps(an, ensure_ascii=False)

    # оригинал
    async def viewed() -> Any:
        return (await su(w.engine, "SELECT original_viewed_at FROM partner_messages WHERE id = :m",
                         m=m1))[0][0]  # fmt: skip

    r = await w.http.get(f"{V1}/couple/messages/{m1}/original", headers=w.anna.h)
    assert (
        r.status_code == 200 and r.json()["text"] == "ОРИГИНАЛ-АННЫ-a8" and await viewed() is None
    )
    r = await w.http.get(f"{V1}/couple/messages/{m1}/original", headers=w.igor.h)
    assert r.status_code == 200 and r.json()["text"] == "ОРИГИНАЛ-АННЫ-a8"
    assert r.headers["cache-control"] == "no-store"
    t1 = await viewed()
    assert t1 is not None
    await asyncio.sleep(0.05)
    assert (
        await w.http.get(f"{V1}/couple/messages/{m1}/original", headers=w.igor.h)
    ).status_code == 200
    assert await viewed() == t1
    for p, mid in ((w.igor, held), (w.pavel, m1)):
        assert (
            await w.http.get(f"{V1}/couple/messages/{mid}/original", headers=p.h)
        ).status_code == 404
    # прочитано
    assert (
        await w.http.post(f"{V1}/couple/messages/{m1}/read", headers=w.anna.h)
    ).status_code == 404
    for _ in range(2):
        assert (
            await w.http.post(f"{V1}/couple/messages/{m1}/read", headers=w.igor.h)
        ).status_code == 204
    st = await su(
        w.engine, "SELECT status::text, read_at FROM partner_messages WHERE id = :m", m=m1
    )
    assert st[0][0] == "read" and st[0][1] is not None
    logs = "\n".join(x.getMessage() for x in caplog.records if not x.name.startswith("httpx"))
    assert "ОРИГИНАЛ-АННЫ" not in logs and "АДАПТИРОВАНО" not in logs and not UUID_RE.search(logs)


async def test_original_unavailable_not_recorded_and_409(
    w: W, monkeypatch: pytest.MonkeyPatch
) -> None:
    """АР-40: оригинал не расшифровался — 200 `UnavailableText`, просмотр **не** фиксируется; в
    ленте нерасшифрованная версия — `UnavailableText`; 40P01 — 409 `conflict_retry`."""
    m1 = await _sent(w, w.anna, "БУДЕТ-ИСПОРЧЕН-a8")
    await deliver(w, m1, "версия", adapted=True)
    await su(w.engine, "UPDATE partner_messages SET original_enc = :b, adapted_enc = :b "
             "WHERE id = :m", b=b"\x00bad", m=m1)  # fmt: skip
    r = await w.http.get(f"{V1}/couple/messages/{m1}/original", headers=w.igor.h)
    assert r.status_code == 200 and r.json()["text"]["unavailable"] is True
    assert (await su(w.engine, "SELECT original_viewed_at FROM partner_messages WHERE id = :m",
                     m=m1))[0][0] is None  # fmt: skip
    item = (await feed(w, w.igor))["items"][0]
    assert item["text"]["unavailable"] is True

    class Deadlock(Exception):
        sqlstate = "40P01"

    async def boom(*a: Any, **k: Any) -> Any:
        raise DBAPIError("SELECT", {}, Deadlock())

    monkeypatch.setattr(cm_repo, "read_original", boom)
    r = await w.http.get(f"{V1}/couple/messages/{m1}/original", headers=w.igor.h)
    assert r.status_code == 409 and r.json()["code"] == "conflict_retry"


async def test_feed_cursor_and_equal_sent_at(w: W) -> None:
    """3.3, 0012: курсор — без повторов и пропусков при одинаковом `sent_at` на границе страниц;
    подделанный, испорченный и чужой (выданный партнёру) — 422; `limit` вне 1..100 — 422."""
    same = datetime.now(UTC) - timedelta(minutes=5)
    async with w.engine.begin() as c:
        ids = [await _pmsg(c, w.couple, (w.igor.uid, w.igor.agent), w.anna.uid, f"m{i}",
                           "delivered", same, for_recipient=f"m{i}") for i in range(7)]  # fmt: skip
    seen: list[str] = []
    cur: str | None = None
    while True:
        page = await feed(w, w.anna, limit=2, **({"cursor": cur} if cur else {}))
        seen += [i["id"] for i in page["items"]]
        cur = page["next_cursor"]
        if not cur:
            break
    assert sorted(seen) == sorted(str(i) for i in ids) and len(seen) == len(set(seen))
    first = await feed(w, w.anna, limit=2)
    c_anna = first["next_cursor"]
    head, _sig = c_anna.split(".")
    for bad in (c_anna + "x", f"{head}.AAAA", "x.y.z", "", "..."):
        assert (await w.http.get(f"{V1}/couple/messages", headers=w.anna.h,
                                 params={"cursor": bad})).status_code == 422, bad  # fmt: skip
    assert (await w.http.get(f"{V1}/couple/messages", headers=w.igor.h,
                             params={"cursor": c_anna})).status_code == 422  # fmt: skip
    for lim in (0, 101):
        assert (await w.http.get(f"{V1}/couple/messages", headers=w.anna.h,
                                 params={"limit": lim})).status_code == 422  # fmt: skip


async def test_ex_couple_feed_empty(w: W) -> None:
    """АР-40, ТЗ §4.5 п.2: после выхода из пары лента пуста, оригинал и «прочитано» — 404, для
    обоих; бывшая пара (Анна–Павел) в ленте не появляется."""
    m1 = await _sent(w, w.anna, "ДО-ВЫХОДА-a8")
    await deliver(w, m1, "до выхода", adapted=False)
    async with w.engine.begin() as c:
        await _pmsg(c, w.ex_couple, (w.pavel.uid, w.pavel.agent), w.anna.uid, "СТАРОЕ-ПАВЛА-a8",
                    "delivered", datetime.now(UTC), for_recipient="СТАРОЕ-ПАВЛА-a8")  # fmt: skip
    assert "СТАРОЕ-ПАВЛА" not in json.dumps(await feed(w, w.anna), ensure_ascii=False)
    assert (await w.http.post(f"{V1}/couple/leave", headers=w.igor.h)).status_code == 204
    for p in (w.anna, w.igor):
        assert (await feed(w, p))["items"] == []
        assert (
            await w.http.get(f"{V1}/couple/messages/{m1}/original", headers=p.h)
        ).status_code == 404
        assert (
            await w.http.post(f"{V1}/couple/messages/{m1}/read", headers=p.h)
        ).status_code == 404
    assert (await feed(w, w.pavel))["items"] == []


async def test_drafts_logs_no_texts(w: W, caplog: pytest.LogCaptureFixture) -> None:
    """Журналы `/drafts*`: только событие и код — без текстов черновиков, разговора и id."""
    caplog.set_level(logging.DEBUG)
    w.model.drafts = [_draft("СЕКРЕТ-ЧЕРНОВИКА-a8")]
    d = (await create(w, w.anna)).json()
    await w.http.patch(f"{V1}/drafts/{d['id']}", headers=w.anna.h,
                       json={"text": "СЕКРЕТ-ПРАВКИ-a8", "expected_version": 1})  # fmt: skip
    await send(w, w.anna, d["id"], 2)
    w.model.drafts = [LLMUnavailable("x")]
    await create(w, w.anna)
    logs = "\n".join(x.getMessage() for x in caplog.records if not x.name.startswith("httpx"))
    assert "СЕКРЕТ" not in logs and "опаздывает" not in logs and not UUID_RE.search(logs)
    sent_model = json.dumps([c["payload"] for c in w.model.calls], ensure_ascii=False)
    assert "Мне тяжело с её упрёками" not in sent_model  # разговор партнёра — не во входе модели

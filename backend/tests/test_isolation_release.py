"""4.3 (A8): набор выпуска — сквозной сценарий утечки «Анна → Игорь» по всем каналам сразу и страж
полноты перечня личных таблиц и функций (ТЗ §6, §6.1, §6.2, §6.3 п.5, §7, §9.1 п.5, §9.2 п.1–4,
§12 п.3; ФТ-05, ФТ-07…ФТ-14, ФТ-18, ФТ-21; АР-10, АР-11, АР-24; карточка 4.3 п.2, п.7).

**Сквозной сценарий** (п.2). У автора — уникальные метки в личном чате (реплики и ответы агента),
сводке диалога, сводке профиля, памяти всех 6 типов, удалённой записи памяти, черновиках
(неотправленном, отклонённом), остановленном сообщении (``withheld``), сообщении в ``processing``.
Затем полный цикл как в продукте: «Отправить» по HTTP → настоящий воркер конвейера → настоящий
воркер уведомлений (Bot API подменён) → извлечение памяти получателя → чат получателя → черновик
ответа → лента и оригинал → выгрузка → сжатие памяти и истории получателя. Метки автора, кроме
одобренного оригинала и доставленной версии, не должны встретиться ни в одном ответе API
получателю, ни в одном входе модели и эмбеддера от его имени (заглушка ``Rec`` ещё и повторяет
увиденные метки в ответах — утечка во вход стала бы видна и в выходах), ни в запросе к Telegram,
ни в журналах, ни в ``audit_log``/``safety_events``. И обратно (Игорь → Анна). Бывший партнёр и
посторонняя — не видят ни одной метки вообще.

Входы модели конвейера — отдельно: агент получателя видит только одобренный оригинал (§6.2), в
том числе остановленного сообщения (его автор тоже нажал «Отправить»), и ничего из личного.

**Страж полноты** (п.7): перечень личных таблиц и функций ``SECURITY DEFINER`` сверяется со схемой
(``information_schema``/``pg_catalog``); новая таблица с ``user_id``/``couple_id``/``agent_id``…
или новая функция без записи в перечне — тест красный. Для каждой личной таблицы проверяется
поведение: RLS включён, у постороннего (``feelings_app`` с чужим ``app.user_id``) и у анонима —
0 строк при наличии строк; у функций — ``EXECUTE`` только у своей роли, у ``PUBLIC`` — нет.

Данные выдуманные, сеть не используется. Дефекты — ``xfail(strict=True)`` (docs/reviews/4.3-test.md)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.ai import memory as mem_ai
from app.ai.embeddings import HashingEmbedder, to_pgvector
from app.ai.prompts import V0
from app.ai.summary import CompressionPolicy
from app.api.chat import ChatRuntime
from app.config import get_settings
from app.core import crypto
from app.core.limits import MemoryDailyLimit
from app.db import make_sessionmaker, user_scope
from app.main import app
from app.repositories import memory_work as mw
from app.repositories.chat import HistorySummary, pack_summary
from app.workers.pipeline import PipelineSettings, PipelineWorker
from tests._isolation_kit import (
    V1,
    Person,
    Rec,
    RecEmbedder,
    dump,
    enter,
    mark,
    marks_in,
    pair_up,
    settings,
    su,
)
from tests._isolation_set import ISOLATION_MIN
from tests.conftest import BACKEND_DIR, TEST_FP
from tests.test_chat_a8 import KEYS, _pmsg
from tests.test_chat_api_a8 import call, stream_ok
from tests.test_notify_worker import NEW_MESSAGE_TEXT, FakeTg, chat_of, nworker
from tests.test_pipeline_db import _park
from tests.test_rls_isolation import World

pytestmark = pytest.mark.isolation

SM = async_sessionmaker[AsyncSession]
CHAT = f"{V1}/chat/messages"
GAP = timedelta(minutes=1)
LEASE = timedelta(seconds=2)  # чужие агенты, захваченные попутно, освобождаются быстро


# =============================================================== окружение


@dataclass
class Env:
    http: httpx.AsyncClient
    engine: AsyncEngine
    sm: SM
    rt: ChatRuntime
    seen: dict[str, list[str]] = field(default_factory=dict)  # ответы API по людям
    people: list[Person] = field(default_factory=list)

    async def enter(self, label: str) -> Person:
        p = await enter(self.http, self.engine, label)
        self.people.append(p)
        return p

    def act(self, rec: Rec, emb: RecEmbedder) -> None:
        """Дальше модели и эмбеддер приложения — от имени этого человека."""
        self.rt.models = rec.models()
        self.rt.embedder = emb

    async def req(self, p: Person, method: str, path: str, **kw: Any) -> httpx.Response:
        r = await self.http.request(method, f"{V1}{path}", headers=p.h, **kw)
        self.seen.setdefault(p.label, []).append(f"{method} {path} {r.status_code} {r.text}")
        return r

    async def chat(self, p: Person, body: str) -> None:
        r = await call("POST", CHAT, headers=p.h, body={"text": body})
        evs = stream_ok(r)
        assert evs[-1][0] == "done", evs
        self.seen.setdefault(p.label, []).append(f"POST /chat/messages {r.body}")
        await self.rt.drain()


@pytest.fixture
async def env(engine: AsyncEngine, sessionmaker: SM) -> AsyncIterator[Env]:
    rt = ChatRuntime(models=Rec().models(), limiter=MemoryDailyLimit(),
                     compression=CompressionPolicy(trigger_turns=7, keep_turns=6, batch_turns=4))  # fmt: skip
    saved = (app.state.sessionmaker, app.state.keyring, app.state.chat)
    app.state.sessionmaker, app.state.keyring, app.state.chat = sessionmaker, KEYS, rt
    app.dependency_overrides[get_settings] = settings
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://a8") as http:
        e = Env(http, engine, sessionmaker, rt)
        yield e
        await rt.drain()
        # Удаление аккаунтов (ФТ-21, как в продукте): незавершённые сессии памяти и очереди этих
        # людей не достаются тестам других модулей (общая БД, Б-71).
        for p in e.people:
            r = await http.delete(f"{V1}/me", headers=p.h)
            assert r.status_code in (204, 401), r.text
    await rt.aclose()
    app.dependency_overrides.clear()
    app.state.sessionmaker, app.state.keyring, app.state.chat = saved


# =============================================================== личные данные автора


@dataclass
class Secrets:
    """Метки автора по каналам; ``allowed`` — что получатель видеть вправе."""

    m: dict[str, str]
    msg_approved: uuid.UUID = field(default_factory=uuid.uuid4)
    msg_withheld: uuid.UUID = field(default_factory=uuid.uuid4)
    msg_processing: uuid.UUID = field(default_factory=uuid.uuid4)
    drafts: list[uuid.UUID] = field(default_factory=list)
    memory: list[uuid.UUID] = field(default_factory=list)

    @property
    def allowed(self) -> set[str]:
        return {self.m["APPROVED"], self.m["ADAPTED"]}

    @property
    def forbidden(self) -> set[str]:
        return set(self.m.values()) - self.allowed


MEMORY_KINDS = ("fact", "date", "preference", "sensitive_topic", "observation", "from_partner")
SOURCE = {"observation": "agent_inference", "from_partner": "partner_message"}


async def _memory(
    env: Env, who: Person, kind: str, body: str, src_msg: uuid.UUID | None
) -> uuid.UUID:
    mid = uuid.uuid4()
    [vec] = await HashingEmbedder().embed([body])
    await su(env.engine,
             "INSERT INTO memory_items (id, fingerprint, agent_id, type, text_enc, source, "
             "source_message_id, confidence, embedding, embedding_model) VALUES (:i, "
             "sha256(convert_to(CAST(gen_random_uuid() AS text), 'UTF8')), :a, "
             "CAST(:t AS memory_type), :x, CAST(:s AS memory_source), :sm, 0.5, "
             "CAST(:v AS vector), :em)",
             i=mid, a=who.agent, t=kind, x=KEYS.seal(crypto.MEMORY_TEXT, mid, body),
             s=SOURCE.get(kind, "owner_dialog"), sm=src_msg if kind == "from_partner" else None,
             v=to_pgvector(vec), em=HashingEmbedder.MODEL)  # fmt: skip
    return mid


async def _draft(env: Env, who: Person, rec: Rec, body: str) -> dict[str, Any]:
    rec.draft_texts.append(body)
    r = await env.req(who, "POST", "/drafts", json={})
    assert r.status_code == 201, r.text
    assert r.json()["text"] == body
    return dict(r.json())


async def _send(env: Env, who: Person, d: dict[str, Any]) -> uuid.UUID:
    r = await env.req(who, "POST", f"/drafts/{d['id']}/send",
                      json={"expected_version": d["version"]})  # fmt: skip
    assert r.status_code == 202, r.text
    return uuid.UUID(r.json()["id"])


async def author_private(env: Env, a: Person, b: Person, couple: uuid.UUID, rec: Rec,
                         emb: RecEmbedder) -> Secrets:  # fmt: skip
    """Всё личное автора — через API и (сводки, память) суперпользователем с шифрованием."""
    kinds = ["CHAT", "CHATTWO", "SUMMARY", "PROFILE", "DELETED", "DRAFT", "REJECTED", "WITHHELD",
             "PROCESSING", "APPROVED", "ADAPTED", *(k.upper().replace("_", "") for k in MEMORY_KINDS)]  # fmt: skip
    s = Secrets({k: mark(k) for k in kinds})
    rec.adapt_mark = s.m["ADAPTED"]
    env.act(rec, emb)
    await env.chat(a, f"Мне тяжело, расскажу только тебе: {s.m['CHAT']}")
    await env.chat(a, f"И ещё одно личное: {s.m['CHATTWO']}")
    conv = (await su(env.engine, "SELECT c.id FROM conversations c WHERE c.agent_id = :a",
                     a=a.agent))[0][0]  # fmt: skip
    # Сообщение партнёра автору — источник записи from_partner (ФТ-14); его текст — партнёра.
    async with env.engine.begin() as c:
        src = await _pmsg(c, couple, (b.uid, b.agent), a.uid, f"Сообщение от {b.label}",
                          "delivered", datetime.now(UTC) - timedelta(hours=3),
                          for_recipient=f"Сообщение от {b.label}")  # fmt: skip
    for kind in MEMORY_KINDS:
        key = kind.upper().replace("_", "")
        s.memory.append(await _memory(env, a, kind, f"{kind}: {s.m[key]}", src))
    gone = await _memory(env, a, "fact", f"удалить: {s.m['DELETED']}", None)
    assert (await env.req(a, "DELETE", f"/memory/{gone}")).status_code == 204
    await env.rt.drain()
    # Сводки — после удаления: очистка АР-37 обнулила бы их (это проверяют тесты 2.4).
    await su(env.engine, "UPDATE agents SET profile_summary_enc = :x WHERE id = :a", a=a.agent,
             x=KEYS.seal(crypto.AGENT_PROFILE_SUMMARY, a.agent, f"Профиль: {s.m['PROFILE']}"))  # fmt: skip
    await su(env.engine, "UPDATE conversations SET summary_enc = :x WHERE id = :c", c=conv,
             x=KEYS.seal(crypto.CONVERSATION_SUMMARY, conv,
                         pack_summary(HistorySummary(f"Сводка: {s.m['SUMMARY']}"))))  # fmt: skip
    s.drafts.append(uuid.UUID((await _draft(env, a, rec, f"Черновик: {s.m['DRAFT']}"))["id"]))
    rej = await _draft(env, a, rec, f"Отклонённый: {s.m['REJECTED']}")
    assert (await env.req(a, "POST", f"/drafts/{rej['id']}/reject")).status_code == 204
    s.drafts.append(uuid.UUID(rej["id"]))
    proc = await _draft(env, a, rec, f"Ждёт конвейера: {s.m['PROCESSING']}")
    s.drafts.append(uuid.UUID(proc["id"]))
    s.msg_processing = await _send(env, a, proc)
    # Остальные строки очередей (в т.ч. «processing» автора выше) — под долгой арендой: настоящие
    # воркеры захватят только два сообщения этого теста.
    await _park(env.engine)
    wh = await _draft(env, a, rec, f"Ещё раз — пожалеешь. {s.m['WITHHELD']}")
    ok = await _draft(env, a, rec, f"Ты опять опоздал! {s.m['APPROVED']}")
    s.drafts += [uuid.UUID(wh["id"]), uuid.UUID(ok["id"])]
    s.msg_withheld = await _send(env, a, wh)
    s.msg_approved = await _send(env, a, ok)
    return s


# =============================================================== цикл получателя


_LEASE_COLS = (
    "memory_lease_token, memory_lease_kind, memory_lease_at, memory_lease_until, memory_attempts"
)


async def _claim_mine(engine: AsyncEngine, who: Person, kind: str) -> Any:
    """Настоящий захват (``memory_claim_*``), пока в выдаче не окажется агент этого человека.
    Чужих агентов (других тестов), захваченных попутно, возвращает в состояние до захвата —
    иначе их истёкшие аренды достались бы тестам других модулей (общая БД, Б-71)."""
    wsm = make_sessionmaker(engine)
    snap = {r[0]: tuple(r[1:]) for r in await su(engine, f"SELECT id, {_LEASE_COLS} FROM agents")}
    taken: set[uuid.UUID] = set()
    try:
        for _ in range(100):
            if kind == "extract":
                claims: list[Any] = await mw.claim_extraction(wsm, gap=GAP, lease=LEASE, limit=100)
            else:
                claims = await mw.claim_compaction(wsm, every=timedelta(days=7), lease=LEASE,
                                                   limit=100)  # fmt: skip
            mine = [c for c in claims if c.agent_id == who.agent]
            taken |= {c.agent_id for c in claims if c.agent_id != who.agent}
            if mine or not claims:
                return mine[0] if mine else None
        raise AssertionError("слишком много кандидатов")
    finally:
        for agent in taken & set(snap):
            tok, k, at_, until, attempts = snap[agent]
            await su(engine, "UPDATE agents SET memory_lease_token = :t, memory_lease_kind = :k, "
                     "memory_lease_at = :a, memory_lease_until = :u, memory_attempts = :n "
                     "WHERE id = :i", t=tok, k=k, a=at_, u=until, n=attempts, i=agent)  # fmt: skip


async def _claim_extraction(engine: AsyncEngine, who: Person) -> Any:
    return await _claim_mine(engine, who, "extract")


async def _claim_compaction(engine: AsyncEngine, who: Person) -> Any:
    return await _claim_mine(engine, who, "compact")


async def _backdate_chat(engine: AsyncEngine, who: Person) -> None:
    """Сессия получателя завершена (пауза больше ``GAP``) — извлечение её захватит."""
    await su(engine, "UPDATE conversation_messages m SET created_at = m.created_at - "
             "interval '2 hours' FROM conversations c WHERE c.id = m.conversation_id "
             "AND c.agent_id = :a", a=who.agent)  # fmt: skip


async def _private_db_texts(env: Env, who: Person) -> str:
    """Всё личное человека в БД, расшифрованное (память, сводки, черновики, реплики)."""
    e = env.engine
    out: list[str] = []
    for r in await su(e, "SELECT id, text_enc FROM memory_items WHERE agent_id = :a AND "
                      "text_enc IS NOT NULL", a=who.agent):  # fmt: skip
        out.append(KEYS.open(crypto.MEMORY_TEXT, r.id, r.text_enc))
    for r in await su(
        e, "SELECT id, text_enc FROM message_drafts WHERE agent_id = :a", a=who.agent
    ):
        out.append(KEYS.open(crypto.DRAFT_TEXT, r.id, r.text_enc))
    for r in await su(e, "SELECT m.id, m.text_enc FROM conversation_messages m JOIN conversations c "
                      "ON c.id = m.conversation_id WHERE c.agent_id = :a", a=who.agent):  # fmt: skip
        out.append(KEYS.open(crypto.CONVERSATION_MESSAGE_TEXT, r.id, r.text_enc))
    for r in await su(e, "SELECT id, summary_enc FROM conversations WHERE agent_id = :a AND "
                      "summary_enc IS NOT NULL", a=who.agent):  # fmt: skip
        out.append(KEYS.open(crypto.CONVERSATION_SUMMARY, r.id, r.summary_enc))
    for r in await su(e, "SELECT profile_summary_enc FROM agents WHERE id = :a AND "
                      "profile_summary_enc IS NOT NULL", a=who.agent):  # fmt: skip
        out.append(KEYS.open(crypto.AGENT_PROFILE_SUMMARY, who.agent, r.profile_summary_enc))
    return "\n".join(out)


async def browse(env: Env, p: Person, s: Secrets) -> None:
    """Каждый канал API, доступный человеку, включая попытки по чужим id."""
    for path in ("/me", "/couple", "/couple/messages", "/memory", "/chat/messages", "/me/export",
                 "/couple/subscription", "/help/contacts"):  # fmt: skip
        await env.req(p, "GET", path)
    for mid in (s.msg_approved, s.msg_withheld, s.msg_processing):
        await env.req(p, "GET", f"/couple/messages/{mid}/original")
    for did in s.drafts:
        assert (await env.req(p, "GET", f"/drafts/{did}")).status_code == 404
        assert (await env.req(p, "POST", f"/drafts/{did}/send",
                              json={"expected_version": 1})).status_code == 404  # fmt: skip
    for item in s.memory:
        assert (await env.req(p, "DELETE", f"/memory/{item}")).status_code == 404


@pytest.mark.parametrize("direction", ["anna_to_igor", "igor_to_anna"])
async def test_end_to_end_no_leak_on_any_channel(
    env: Env, caplog: pytest.LogCaptureFixture, direction: str
) -> None:
    """Критерий RoadMap 4.3 и §12 п.3; ТЗ §2 п.4, §6, §6.1, §6.2, §6.3 п.5, §9.1 п.5, §9.2 п.1:
    ни одна метка личного автора (кроме одобренного оригинала и доставленной версии) не доходит
    до партнёра ни по одному каналу — API, входы моделей и эмбеддера, бот, журналы,
    ``audit_log``/``safety_events``, выгрузка; бывший партнёр и посторонняя не видят ни одной."""
    caplog.set_level(logging.DEBUG)
    anna = await env.enter("Анна")
    igor = await env.enter("Игорь")
    pavel = await env.enter("Павел")  # бывший партнёр Анны
    olga = await env.enter("Ольга")  # посторонняя
    await pair_up(env.engine, anna, pavel, "dissolved")
    couple = await pair_up(env.engine, anna, igor)
    a, b = (anna, igor) if direction == "anna_to_igor" else (igor, anna)
    rec_a, emb_a = Rec("author"), RecEmbedder()
    s = await author_private(env, a, b, couple, rec_a, emb_a)

    # ---- конвейер (агент получателя): настоящий воркер, только два сообщения этого теста
    rec_pipe = Rec("pipeline", adapt_mark=s.m["ADAPTED"])
    worker = PipelineWorker(worker_sm=make_sessionmaker(env.engine), keys=KEYS,
                            models=rec_pipe.models(), prompt_set=V0,
                            settings=PipelineSettings(lease=timedelta(minutes=5), batch=20))  # fmt: skip
    out = await worker.run_once()
    assert sorted(o.status for o in out) == ["delivered", "withheld"], out
    for c in rec_pipe.calls:  # §6.2: только одобренный оригинал (и его версии), ничего личного
        seen = marks_in("\n".join(t for _, t in c["messages"]))
        assert seen <= {s.m["APPROVED"], s.m["ADAPTED"], s.m["WITHHELD"]}, (c["role"], seen)
    status = {r.id: r.status for r in await su(env.engine, "SELECT id, status::text AS status FROM "
                                               "partner_messages WHERE id IN (:x, :y, :z)",
                                               x=s.msg_approved, y=s.msg_withheld,
                                               z=s.msg_processing)}  # fmt: skip
    assert status == {s.msg_approved: "delivered", s.msg_withheld: "withheld",
                      s.msg_processing: "processing"}  # fmt: skip

    # ---- уведомление: настоящий воркер, Bot API подменён
    tg = FakeTg()
    nw_ = nworker(env.engine, tg)
    notified = await nw_.run_once()
    await nw_.api.aclose()
    assert [o.status for o in notified] == ["sent"]
    assert len(tg.requests) == 1
    assert tg.requests[0]["chat_id"] == await chat_of(env.engine, b.uid)
    assert tg.requests[0]["text"] == NEW_MESSAGE_TEXT
    assert marks_in(dump(tg.requests)) == set()

    # ---- получатель: чат, память, черновик ответа, лента, выгрузка, сжатие
    rec_b, emb_b = Rec("recipient"), RecEmbedder()
    env.act(rec_b, emb_b)
    own_b = {k: mark(k) for k in ("BCHAT", "BCHATTWO", "BCHATTHREE")}
    await env.chat(b, f"Получил сообщение, обсудим: {own_b['BCHAT']}")
    await env.chat(b, f"Что мне ответить? {own_b['BCHATTWO']}")
    await _backdate_chat(env.engine, b)
    await su(env.engine, "UPDATE partner_messages SET sent_at = sent_at - interval '2 hours', "
             "delivered_at = delivered_at - interval '2 hours' WHERE id = :m", m=s.msg_approved)  # fmt: skip
    claim = await _claim_extraction(env.engine, b)
    assert claim is not None
    done = await mem_ai.run_extraction(env.sm, KEYS, TEST_FP, rec_b.models(), claim, embedder=emb_b)
    assert done.status == "done" and done.inserted >= 1, done
    kinds = {r[0] for r in await su(env.engine, "SELECT type::text FROM memory_items WHERE "
                                   "agent_id = :a", a=b.agent)}  # fmt: skip
    assert "from_partner" in kinds  # одобренное сообщение пары дошло до памяти получателя
    await env.chat(b, f"Помоги подготовить ответ. {own_b['BCHATTHREE']}")
    for _ in range(2):  # 10 реплик > порога 7 — фоновое сжатие истории получателя
        await env.chat(b, "Продолжим разговор.")
    r = await env.req(b, "POST", "/drafts", json={"reply_to_id": str(s.msg_approved)})
    assert r.status_code == 201, r.text
    reply = r.json()
    assert (await env.req(b, "POST", f"/drafts/{reply['id']}/regenerate")).status_code == 200
    feed = (await env.req(b, "GET", "/couple/messages")).json()
    incoming = [i for i in feed["items"] if i["direction"] == "incoming"]
    assert [i["id"] for i in incoming] == [str(s.msg_approved)]  # остановленного и processing нет
    assert incoming[0]["is_adapted"] is True  # доставлена адаптация с пометкой (§6.2 п.7)
    assert s.m["ADAPTED"] in dump(incoming[0]["text"])
    r = await env.req(b, "GET", f"/couple/messages/{s.msg_approved}/original")
    assert r.status_code == 200 and s.m["APPROVED"] in r.text
    for hidden in (s.msg_withheld, s.msg_processing):
        assert (await env.req(b, "GET", f"/couple/messages/{hidden}/original")).status_code == 404
    comp = await _claim_compaction(env.engine, b)
    assert comp is not None
    cdone = await mem_ai.run_compaction(env.sm, KEYS, TEST_FP, rec_b.models(), comp, embedder=emb_b)
    assert cdone.status == "done", cdone
    await env.rt.drain()
    await browse(env, b, s)
    assert any(c["role"] == "summary" for c in rec_b.calls)  # сжатие истории получателя прошло
    assert {"dialog_stream", "draft", "memory_extract", "profile_summary"} <= set(rec_b.roles())

    # ---- бывший партнёр и посторонняя: чат (свой агент) и все каналы API
    rec_x, emb_x = Rec("ex_outsider"), RecEmbedder()
    env.act(rec_x, emb_x)
    for p in (pavel, olga):
        await env.chat(p, f"Своё: {mark('OTHER')}")
        await browse(env, p, s)

    # ---- проверки: получатель
    forbidden = s.forbidden
    to_b = "\n".join(env.seen[b.label])
    assert forbidden & marks_in(to_b) == set(), "API получателю"
    assert forbidden & marks_in(rec_b.text()) == set(), "входы модели получателя"
    assert forbidden & marks_in("\n".join(emb_b.inputs)) == set(), "входы эмбеддера получателя"
    assert forbidden & marks_in(await _private_db_texts(env, b)) == set(), "личное получателя в БД"
    assert s.m["APPROVED"] in to_b and s.m["ADAPTED"] in to_b  # одобренное — дошло
    # ---- бывший партнёр и посторонняя — ни одной метки автора
    for p in (pavel, olga):
        assert set(s.m.values()) & marks_in("\n".join(env.seen[p.label])) == set(), p.label
    assert set(s.m.values()) & marks_in(rec_x.text() + "\n".join(emb_x.inputs)) == set()
    # ---- журналы (ни одной метки — даже одобренной: §9.1 п.5) и audit_log/safety_events
    assert marks_in(caplog.text) == set(), "журналы"
    rows = await su(env.engine, "SELECT * FROM audit_log") + await su(env.engine,
                                                                     "SELECT * FROM safety_events")  # fmt: skip
    assert marks_in(dump([tuple(r) for r in rows])) == set()
    # ---- автор видит своё (проверка, что метки действительно заведены, а тест не пустой)
    a_export = (await env.req(a, "GET", "/me/export")).text
    for k in ("CHAT", "SUMMARY", "PROFILE", "DRAFT", "REJECTED", "WITHHELD", "PROCESSING", "FACT"):
        assert s.m[k] in a_export, k
    assert s.m["DELETED"] not in a_export  # удалённое не возвращается (ФТ-13)
    assert s.m["CHAT"] in rec_a.text()  # метки действительно уходили в модель автора


# =============================================================== страж полноты (п.7)

# Личные таблицы §7 → столбец владельца, по которому RLS режет строки. Новая таблица с одним из
# OWNER_COLUMNS — сюда (и в тесты изоляции), иначе страж красный.
PRIVATE_TABLES: dict[str, str] = {
    "users": "id",
    "agents": "user_id",
    "conversations": "agent_id",
    "conversation_messages": "conversation_id",
    "memory_items": "agent_id",
    "message_drafts": "agent_id",
    "couples": "id",
    "couple_members": "couple_id",
    "invites": "couple_id",
    "partner_messages": "couple_id",
    "subscriptions": "couple_id",
    "payments": "couple_id",
    "safety_events": "user_id",
    "audit_log": "actor_id",
}
# Справочные таблицы без личных данных (§6.4, §10) — RLS не нужен; доступ проверяют свои тесты.
REFERENCE_TABLES = {"kb_documents", "kb_chunks", "prompt_versions", "alembic_version"}
OWNER_COLUMNS = ("user_id", "couple_id", "agent_id", "conversation_id", "author_id",
                 "recipient_id", "created_by", "actor_id", "payer_id", "telegram_id")  # fmt: skip

# Функции SECURITY DEFINER → единственная роль с EXECUTE. Новая — сюда и в тесты, иначе красный.
_APP = ("accept_invite", "couple_feed", "couple_feed_page", "couple_members", "create_couple",
        "delete_account", "delete_memory", "export_partner_messages", "export_safety_events",
        "find_invite", "has_access", "is_active_member", "is_member", "issue_invite",
        "kb_nearest", "leave_couple", "mark_read", "memory_compaction_done",
        "memory_extraction_done", "memory_forgotten", "memory_nearest", "read_original",
        "send_draft", "set_memory_embedding", "subscription_create_invoice",
        "subscription_start_trial", "subscription_status", "upsert_user")  # fmt: skip
_WORKER = ("kb_add_chunk", "kb_claim_index", "kb_fail_index", "kb_finish_index", "kb_read_source",
           "kb_set_chunk_embedding", "memory_claim_compaction", "memory_claim_extraction",
           "pipeline_claim", "pipeline_claim_notify", "pipeline_deliver_leased",
           "pipeline_notify_done", "pipeline_notify_release", "pipeline_read", "pipeline_release",
           "pipeline_withhold_leased")  # fmt: skip
SECURITY_DEFINER: dict[str, str] = {
    **{f"app_{n}": "feelings_app" for n in _APP},
    **{n: "feelings_worker" for n in _WORKER},
    "admin_kb_upload": "feelings_admin",
    "admin_kb_delete": "feelings_admin",
    **{
        f"app_bot_payment_{n}": "feelings_billing"
        for n in ("credit", "lookup", "precheck", "refunded")
    },
}
ROLES = ("feelings_app", "feelings_api", "feelings_worker", "feelings_admin", "feelings_billing")


async def test_completeness_guard_tables_match_schema(engine: AsyncEngine) -> None:
    """Карточка п.7, ТЗ §7, §9.2 п.4: каждая таблица схемы классифицирована; каждая таблица со
    столбцом владельца — в перечне личных; у каждой личной включён RLS и есть политика."""
    async with engine.connect() as c:
        tables = {r[0] for r in await c.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}  # fmt: skip
        owned = {r[0] for r in await c.execute(text(
            "SELECT DISTINCT table_name FROM information_schema.columns WHERE table_schema = "
            "'public' AND column_name = ANY(:c)"), {"c": list(OWNER_COLUMNS)})}  # fmt: skip
        rls = {r[0]: (r[1], r[2]) for r in await c.execute(text(
            "SELECT c.relname, c.relrowsecurity, (SELECT count(*) FROM pg_policy p "
            "WHERE p.polrelid = c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid = "
            "c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r'"))}  # fmt: skip
    assert tables == set(PRIVATE_TABLES) | REFERENCE_TABLES, "новая таблица не классифицирована"
    assert owned <= set(PRIVATE_TABLES), (
        f"личная таблица вне перечня: {owned - set(PRIVATE_TABLES)}"
    )
    for t in PRIVATE_TABLES:
        enabled, policies = rls[t]
        assert enabled and policies >= 1, f"{t}: RLS выключен или нет политики"


async def test_completeness_guard_functions_match_schema(engine: AsyncEngine) -> None:
    """Карточка п.7, АР-10, АР-11: перечень функций SECURITY DEFINER совпадает со схемой;
    ``EXECUTE`` — только у своей роли (у ``PUBLIC`` и остальных ролей — нет). Б-11:
    администратору не доступна ни одна пользовательская функция ``app_*``."""
    async with engine.connect() as c:
        rows = (await c.execute(text(
            "SELECT p.oid, p.proname, has_function_privilege('public', p.oid, 'EXECUTE') AS pub "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.prosecdef"))).all()  # fmt: skip
        assert sorted(r.proname for r in rows) == sorted(SECURITY_DEFINER), "перечень функций"
        for r in rows:
            assert r.pub is False, r.proname
            who = {role for role in ROLES if (await c.execute(text(
                "SELECT has_function_privilege(:r, CAST(:o AS oid), 'EXECUTE')"),
                {"r": role, "o": r.oid})).scalar_one()}  # fmt: skip
            # Роль входа feelings_api — член без наследования (АР-10): права только после SET ROLE.
            assert who == {SECURITY_DEFINER[r.proname]}, (r.proname, who)


@pytest.mark.parametrize("table", sorted(PRIVATE_TABLES))
async def test_completeness_guard_outsider_and_anonymous_see_no_rows(
    world: World, engine: AsyncEngine, sessionmaker: SM, table: str
) -> None:
    """Карточка п.7 (поведение, а не только флаг): в таблице есть строки (пара Анна — Игорь из
    0.7, подписка, платёж, событие, запись журнала), а посторонний и аноним под ``feelings_app``
    не видят ни одной; снятие политики с любой таблицы делает тест красным."""
    async with engine.begin() as c:
        sub = (await c.execute(text(
            "INSERT INTO subscriptions (couple_id, payer_id, plan, status, period_start, "
            "period_end) VALUES (:k, :u, 'a8', 'active', now(), now() + interval '1 day') "
            "RETURNING id"), {"k": world.couple_id, "u": world.a.user_id})).scalar_one()  # fmt: skip
        await c.execute(text("INSERT INTO payments (subscription_id, couple_id, amount, provider, "
                             "status) VALUES (:s, :k, 1, 'a8', 'succeeded')"),
                        {"s": sub, "k": world.couple_id})  # fmt: skip
        await c.execute(text("INSERT INTO audit_log (actor_id, action, object_type) VALUES "
                             "(:u, 'a8_guard', 'test')"), {"u": world.a.user_id})  # fmt: skip
        await c.execute(text("INSERT INTO safety_events (user_id, type) VALUES (:u, 'violence')"),
                        {"u": world.a.user_id})  # fmt: skip
        total = (await c.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
    try:
        assert total > 0, f"{table}: нет строк — проверка была бы пустой"
        await _nobody_sees(sessionmaker, world, table)
    finally:  # незавершённые сессии памяти мира 0.7 не достаются другим модулям (Б-71)
        async with engine.begin() as c:
            await c.execute(text("DELETE FROM couples WHERE id = :k"), {"k": world.couple_id})
            await c.execute(text("DELETE FROM users WHERE id IN (:a, :b, :c)"),
                            {"a": world.a.user_id, "b": world.b.user_id, "c": world.c.user_id})  # fmt: skip


async def _nobody_sees(sessionmaker: SM, world: World, table: str) -> None:
    for uid in (uuid.uuid4(), world.c.user_id, None):
        async with user_scope(sessionmaker, uid) as s:
            try:
                async with s.begin_nested():
                    n = (await s.execute(text(f"SELECT count(*) FROM {table} WHERE "
                                              f"{PRIVATE_TABLES[table]} IN (:a, :k, :ag, :cv)"),
                                         {"a": world.a.user_id, "k": world.couple_id,
                                          "ag": world.a.agent_id,
                                          "cv": world.a.conversation_id})).scalar_one()  # fmt: skip
            except DBAPIError as exc:  # нет права SELECT — тоже «не видит»
                assert "permission denied" in str(exc), exc
                continue
        assert n == 0, f"{table}: {uid or 'аноним'} видит {n} строк пары Анна — Игорь"


# =============================================================== страж шага CI (§9.2 п.4)

PROBE = "A8_ISOLATION_GUARD_PROBE"


def test_guard_probe() -> None:
    """Проба для стража шага CI: в обычном прогоне проходит; с ``A8_ISOLATION_GUARD_PROBE=skip``
    — пропускается (так выглядел бы тест, которому «не хватило БД»)."""
    if os.environ.get(PROBE) == "skip":
        pytest.skip("проба стража: пропуск")


def _run_release(*args: str, probe: str | None = None) -> subprocess.CompletedProcess[str]:
    env_ = {k: v for k, v in os.environ.items() if k != PROBE}
    if probe is not None:
        env_[PROBE] = probe
    return subprocess.run(  # noqa: S603 — фиксированная команда теста
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "isolation",
         "-k", "test_guard_probe", *args],
        cwd=BACKEND_DIR, env=env_, capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("args", "probe", "code", "says"),
    [
        (("--isolation-min=1",), None, 0, "пропущено 0"),
        (("--isolation-min=1",), "skip", 1, "ПРОПУЩЕН (сборка красная)"),
        (("--isolation-min=2",), None, 4, "нужно не меньше 2"),
    ],
    ids=["green", "skip_is_red", "too_few_is_red"],
)
def test_release_step_guard(args: tuple[str, ...], probe: str | None, code: int, says: str) -> None:
    """Карточка п.1, ТЗ §9.2 п.4: шаг CI ``pytest -m isolation --isolation-min=N`` красный, если
    хоть один тест набора пропущен (skip) или собрано меньше N; без пропусков — зелёный. (0
    собранных — штатный код pytest 5, тоже красный.)"""
    r = _run_release(*args, probe=probe)
    out = r.stdout + r.stderr
    assert r.returncode == code, out[-2000:]
    assert says in out, out[-2000:]


def test_isolation_set_size_is_fixed() -> None:
    """Число N в шаге CI совпадает с зафиксированным в ``tests/_isolation_set.py`` (карточка)."""
    ci = (BACKEND_DIR.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f"--isolation-min={ISOLATION_MIN}" in ci

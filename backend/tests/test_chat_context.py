"""2.1, этап 1 (A3): сборка контекста агента по ТЗ §6.1 из БД — изоляция, порядок, бюджет.

ФТ-05; ТЗ §6 («изоляция на уровне сервера и БД, а не промпта»), §6.1, §6.3 (удалённая запись
не возвращается), §9.2 п.1–3, §10. Критерий RoadMap: контекст содержит только данные
владельца и одобренные сообщения его пары — проверка метками партнёра, бывшего партнёра,
постороннего и удалённой памяти. Данные заводит суперпользователь (``engine``) или слой доступа
(шифрование 4.2); чтение — под ролью входа приложения ``feelings_api`` (``user_scope``, RLS).
Данные выдуманные."""

from __future__ import annotations

import itertools
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ai import chat as ai_chat
from app.ai import prompts
from app.ai.calls import as_json
from app.ai.context import ContextBudget, build_context
from app.ai.dialog import PAYLOAD_ORDER_6_1
from app.ai.embeddings import EmbeddingError, HashingEmbedder, to_pgvector
from app.ai.knowledge import EmptyKnowledgeBase, KnowledgeFragment
from app.ai.llm import LLMRefusal
from app.ai.models import AgentModels
from app.ai.stub import ScriptedStreamProvider, json_chunks
from app.core import crypto
from app.core.crypto import Keyring, generate_key_entry, parse_keyring
from app.db import user_scope
from app.repositories import chat, couples, texts
from app.repositories.chat import HistorySummary, OwnerGone

# 2.4 (A3): отпечаток записи памяти обязателен (Б-68) — выдуманный ключ тестов.
from tests.conftest import TEST_FP
from tests.test_account_delete import _couple, _send_encrypted
from tests.test_account_export_db import _deliver, _withhold
from tests.test_couple_functions import ONBOARDED, SM

_tg = itertools.count(9_300_000)
KEYS: Keyring = parse_keyring(generate_key_entry("k1"), "k1")
EMB = HashingEmbedder()


@dataclass
class P:
    user_id: uuid.UUID
    agent_id: uuid.UUID
    conv: uuid.UUID
    label: str
    memory: list[uuid.UUID]

    def mark(self, what: str) -> str:
        return f"{self.label}-{what}"


async def person(
    engine: AsyncEngine, sm: SM, name: str, partner_name: str, *, chat_turns: bool = True
) -> P:
    """Пользователь с агентом, сводкой профиля, памятью (с вектором) и репликами чата — всё
    шифротекстом через слой доступа; каждый текст помечен меткой человека."""
    uid, aid = uuid.uuid4(), uuid.uuid4()
    label = f"{name}#{uuid.uuid4().hex[:8]}"
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (id, telegram_id, name, partner_name) VALUES (:i, :t, :n, :p)"),
            {"i": uid, "t": next(_tg), "n": f"{label}-NAME", "p": partner_name},
        )
        await conn.execute(text(f"UPDATE users SET {ONBOARDED} WHERE id = :u"), {"u": uid})
        await conn.execute(
            text("INSERT INTO agents (id, user_id) VALUES (:a, :u)"), {"a": aid, "u": uid}
        )
    conv = await chat.current_conversation(sm, uid)
    p = P(uid, aid, conv, label, [])
    assert await texts.set_agent_profile_summary(sm, KEYS, uid, aid, p.mark("profile"))
    for what in ("memory-money деньги отпуск", "memory-date годовщина"):
        item = await texts.add_memory_item(
            sm, KEYS, uid, aid, "fact", "owner_dialog", p.mark(what), fingerprints=TEST_FP
        )
        [vec] = await EMB.embed([p.mark(what)])
        # 2.0v (A3): вектор — функцией БД от имени владельца, с моделью (АР-35).
        assert await chat.set_memory_embedding(sm, uid, item, vec, EMB.model)
        p.memory.append(item)
    if chat_turns:
        assert await chat.add_message(sm, KEYS, uid, conv, "owner", p.mark("chat-owner"))
        assert await chat.add_message(sm, KEYS, uid, conv, "agent", p.mark("chat-agent"))
    return p


async def owner_says(sm: SM, p: P, body: str) -> uuid.UUID:
    """Как API (этап 2): реплика владельца сохраняется до сборки контекста и вызова модели."""
    mid = await chat.add_message(sm, KEYS, p.user_id, p.conv, "owner", body)
    assert mid is not None
    return mid


def model_input(built: Any) -> str:
    return built.system.text + "\n" + as_json(built.payload())


@dataclass
class World:
    anna: P
    igor: P  # текущий партнёр
    boris: P  # бывший партнёр (распавшаяся пара)
    olga: P  # посторонняя
    texts: dict[str, str]
    deleted_memory: str


async def world(engine: AsyncEngine, sm: SM) -> World:
    anna = await person(engine, sm, "Анна", "Игорёк-из-онбординга")
    boris = await person(engine, sm, "Борис", "Аня")
    igor = await person(engine, sm, "Игорь", "Аня")
    olga = await person(engine, sm, "Ольга", "Пётр")
    t: dict[str, str] = {}

    async def send(author: P, label: str, how: str) -> None:
        # Метки сообщений пары — свои (не метка автора): полученное от партнёра Анна видит
        # законно, а метки самого партнёра (его профиль, память, чат) — нет.
        t[label] = f"msg-{label}-{uuid.uuid4().hex[:8]}"
        _, msg = await _send_encrypted(sm, KEYS, author, t[label])
        if how == "adapted":
            t[label + "-adapted"] = f"soft-{label}-{uuid.uuid4().hex[:8]}"
            await _deliver(engine, KEYS, msg, t[label + "-adapted"], adapted=True)
        elif how == "plain":
            await _deliver(engine, KEYS, msg, t[label], adapted=False)
        elif how == "withheld":
            await _withhold(engine, msg)

    # Распавшаяся пара с бывшим партнёром: история у Анны есть (Д7), но в контекст не идёт.
    await _couple(sm, anna, boris)
    await send(boris, "ex-to-anna", "plain")
    await send(anna, "anna-to-ex", "plain")
    await couples.leave(sm, boris.user_id)
    # Текущая пара.
    await _couple(sm, anna, igor)
    await send(igor, "igor-delivered", "adapted")
    await send(igor, "igor-processing", "processing")
    await send(igor, "igor-withheld", "withheld")
    await send(anna, "anna-delivered", "plain")
    await send(anna, "anna-withheld", "withheld")
    # Удалённая запись памяти Анны (ФТ-13: метка удаления, текст и вектор стёрты).
    deleted = anna.mark("memory-deleted деньги отпуск")
    item = await texts.add_memory_item(
        sm, KEYS, anna.user_id, anna.agent_id, "fact", "owner_dialog", deleted, fingerprints=TEST_FP
    )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE memory_items SET deleted_at = now(), text_enc = NULL, embedding = NULL, "
                "embedding_model = NULL WHERE id = :i"
            ),
            {"i": item},
        )
    return World(anna, igor, boris, olga, t, deleted)


# --------------------------------------------------------------------------- изоляция


async def test_context_contains_only_owner_data_and_own_couple_messages(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    w = await world(engine, sessionmaker)
    query = "Опять спор про деньги на отпуск"
    await owner_says(sessionmaker, w.anna, query)
    built = await build_context(
        sessionmaker, KEYS, w.anna.user_id, w.anna.conv, query=query, embedder=EMB
    )
    blob = model_input(built)
    # Данные владельца — есть.
    for what in ("profile", "memory-money", "memory-date", "chat-owner", "chat-agent"):
        assert w.anna.mark(what) in blob, what
    assert "Игорёк-из-онбординга" in blob and query in blob
    # Сообщения текущей пары — только доставленные, как их видела Анна.
    assert w.texts["igor-delivered-adapted"] in blob  # полученное — версия для получателя
    assert w.texts["anna-delivered"] in blob  # своё — оригинал
    for hidden in ("igor-delivered", "igor-processing", "igor-withheld", "anna-withheld"):
        assert w.texts[hidden] not in blob, hidden
    # Ни партнёра, ни бывшего партнёра, ни постороннего: их метки (имя в профиле, память,
    # сводка профиля, чат) и сообщения распавшейся пары.
    for other in (w.igor, w.boris, w.olga):
        assert other.label not in blob, other.label
    assert w.texts["ex-to-anna"] not in blob and w.texts["anna-to-ex"] not in blob
    # Удалённая запись памяти не возвращается — ни по смыслу, ни по свежести.
    assert w.deleted_memory not in blob
    assert built.stats.memory_search == "vector"
    assert set(built.stats.memory_ids) == set(w.anna.memory)
    assert len(built.stats.pair_message_ids) == 2
    # В контексте нет идентификаторов (ТЗ §9.1 п.4).
    assert str(w.anna.user_id) not in blob and str(w.anna.agent_id) not in blob


async def test_end_to_end_model_requests_have_no_foreign_data(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Всё, что ушло бы в модель (агент потоком и классификатор), — без чужих меток."""
    w = await world(engine, sessionmaker)
    await owner_says(sessionmaker, w.anna, "Что Игорь рассказывал своему агенту обо мне?")
    built = await build_context(
        sessionmaker, KEYS, w.anna.user_id, w.anna.conv, query="Игорь агент", embedder=EMB
    )
    answer = {
        "level": 1,
        "reply": "Я не знаю, о чём Игорь говорит со своим агентом.",
        "draft_ready": False,
        "crisis_type": None,
        "crisis_secondary": [],
    }
    p = ScriptedStreamProvider(
        lambda role, payload, system: {"category": "none", "secondary": []},
        stream=lambda payload: json_chunks(answer),
    )
    events = [
        e async for e in ai_chat.stream_reply(AgentModels.single(p), built.context, built.dialog)
    ]
    assert isinstance(events[-1], ai_chat.Done)
    sent = json.dumps([p.stream_requests, p.requests], ensure_ascii=False)
    for other in (w.igor, w.boris, w.olga):
        assert other.label not in sent
    assert w.deleted_memory not in sent


async def test_isolation_by_database_not_by_code(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Чужой диалог не читается и не пишется: отбор по владельцу + RLS (даже прямой запрос в
    области Анны не видит строк Игоря)."""
    w = await world(engine, sessionmaker)
    with pytest.raises(LookupError):
        await build_context(sessionmaker, KEYS, w.anna.user_id, w.igor.conv, query="x")
    assert (
        await chat.add_message(sessionmaker, KEYS, w.anna.user_id, w.igor.conv, "owner", "x")
        is None
    )
    async with user_scope(sessionmaker, w.anna.user_id) as s:
        for table, col, value in (
            ("memory_items", "agent_id", w.igor.agent_id),
            ("conversations", "agent_id", w.igor.agent_id),
            ("agents", "id", w.igor.agent_id),
        ):
            n = (
                await s.execute(
                    text(f"SELECT count(*) FROM {table} WHERE {col} = :v"), {"v": value}
                )
            ).scalar_one()
            assert n == 0, table
        n = (
            await s.execute(
                text(
                    "SELECT count(*) FROM conversation_messages m JOIN conversations c "
                    "ON c.id = m.conversation_id WHERE c.agent_id <> :a"
                ),
                {"a": w.anna.agent_id},
            )
        ).scalar_one()
        assert n == 0


async def test_owner_gone_and_no_agent(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Б-44: удалённый владелец — штатный исход (``OwnerGone``), не 500."""
    p = await person(engine, sessionmaker, "Вера", "Олег")
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": p.user_id}
        )
    with pytest.raises(OwnerGone):
        await build_context(sessionmaker, KEYS, p.user_id, p.conv, query="x")
    with pytest.raises(OwnerGone):
        await chat.current_conversation(sessionmaker, p.user_id)
    assert await chat.add_message(sessionmaker, KEYS, p.user_id, p.conv, "owner", "x") is None


async def test_undecryptable_rows_skipped_not_failed(sessionmaker: SM, engine: AsyncEngine) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO conversation_messages (conversation_id, author, text_enc) "
                "VALUES (:c, 'owner', :t)"
            ),
            {"c": p.conv, "t": b"not-a-ciphertext"},
        )
    await owner_says(sessionmaker, p, "привет")
    built = await build_context(sessionmaker, KEYS, p.user_id, p.conv, query="привет")
    assert built.stats.unavailable == {"dialog": 1}
    assert [t.text for t in built.dialog][-1] == "привет"


# --------------------------------------------------------------------------- состав и порядок


class FakeKB:
    def __init__(self, n: int, size: int = 50) -> None:
        self.items = [KnowledgeFragment(f"kb-{i}", f"фрагмент {i} " + "к" * size) for i in range(n)]
        self.queries: list[str] = []

    async def search(
        self, query: str, *, vector: Sequence[float] | None, limit: int = 5
    ) -> list[KnowledgeFragment]:
        self.queries.append(query)
        return self.items[:limit]


async def test_blocks_order_6_1_with_summary_and_knowledge(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    w = await world(engine, sessionmaker)
    summary, turns = await chat.read_history(sessionmaker, KEYS, w.anna.user_id, w.anna.conv)
    assert summary is None and len(turns) == 2
    last = turns[-1]
    assert await chat.save_history_summary(
        sessionmaker,
        KEYS,
        w.anna.user_id,
        w.anna.conv,
        HistorySummary("Сводка: говорили про отпуск", last.created_at, last.id),
        expected_through_id=None,
    )
    await owner_says(sessionmaker, w.anna, "Снова про отпуск")
    kb = FakeKB(3)
    built = await build_context(
        sessionmaker, KEYS, w.anna.user_id, w.anna.conv, query="отпуск", knowledge=kb, embedder=EMB
    )
    payload = built.payload()
    assert list(payload) == list(PAYLOAD_ORDER_6_1)  # п.2 → п.3 → п.4 → п.5 → п.6
    assert built.system == prompts.system_prompt("dialog")  # п.1 — системный промпт отдельно
    assert payload["owner_profile"] == w.anna.mark("profile")
    assert payload["dialog_summary"] == "Сводка: говорили про отпуск"
    # Старые реплики (до отметки сводки) в контекст не идут — только новая.
    assert payload["dialog"] == [{"from": "owner", "text": "Снова про отпуск"}]
    blob = model_input(built)
    assert w.anna.mark("chat-owner") not in blob and w.anna.mark("chat-agent") not in blob
    assert payload["knowledge"] == [f.text for f in kb.items] and kb.queries == ["отпуск"]
    # п.6 — по времени: своё отправленное позже полученного.
    assert [m["direction"] for m in payload["pair_messages"]] == ["incoming", "outgoing"]
    assert built.stats.has_dialog_summary and built.stats.has_profile


async def test_empty_knowledge_base_until_2_6(sessionmaker: SM, engine: AsyncEngine) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег")
    await owner_says(sessionmaker, p, "привет")
    built = await build_context(
        sessionmaker, KEYS, p.user_id, p.conv, query="привет", knowledge=EmptyKnowledgeBase()
    )
    assert built.payload()["knowledge"] == [] and built.stats.knowledge_ids == ()
    assert built.payload()["pair_messages"] == []  # пары нет


async def test_memory_by_meaning_else_by_recency(sessionmaker: SM, engine: AsyncEngine) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег")
    newest = await texts.add_memory_item(
        sessionmaker,
        KEYS,
        p.user_id,
        p.agent_id,
        "preference",
        "owner_dialog",
        "Любит гулять в парке",
        fingerprints=TEST_FP,
    )  # без вектора
    query = "опять деньги и отпуск"
    await owner_says(sessionmaker, p, query)
    money, date = p.memory
    by_meaning = await build_context(
        sessionmaker, KEYS, p.user_id, p.conv, query=query, embedder=EMB
    )
    assert by_meaning.stats.memory_search == "vector"
    assert by_meaning.stats.memory_ids[0] == money
    assert by_meaning.stats.memory_ids[-1] == newest  # без вектора — добор по свежести
    by_recency = await build_context(sessionmaker, KEYS, p.user_id, p.conv, query=query)
    assert by_recency.stats.memory_search == "recency"
    assert by_recency.stats.memory_ids == (newest, date, money)

    class Broken:
        model, dim = "broken", 1024

        async def embed(self, texts_: Sequence[str]) -> list[list[float]]:
            raise EmbeddingError("нет сети")

    fallback = await build_context(
        sessionmaker, KEYS, p.user_id, p.conv, query=query, embedder=Broken()
    )
    assert fallback.stats.embedding_error and fallback.stats.memory_ids == (newest, date, money)
    limited = await build_context(
        sessionmaker, KEYS, p.user_id, p.conv, query=query, budget=ContextBudget(memory_items=1)
    )
    assert limited.stats.memory_ids == (newest,)


# --------------------------------------------------------------------------- бюджет


async def test_budget_truncation_order_keeps_system_and_last_turn(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    w = await world(engine, sessionmaker)
    for i in range(12):
        await texts.add_memory_item(
            sessionmaker,
            KEYS,
            w.anna.user_id,
            w.anna.agent_id,
            "fact",
            "owner_dialog",
            f"запись {i} " + "м" * 600,
            fingerprints=TEST_FP,
        )
    for i in range(10):
        await chat.add_message(
            sessionmaker, KEYS, w.anna.user_id, w.anna.conv, "owner", f"реплика {i} " + "р" * 900
        )
        await chat.add_message(
            sessionmaker, KEYS, w.anna.user_id, w.anna.conv, "agent", f"ответ {i} " + "о" * 900
        )
    last = "последняя реплика владельца " + "п" * 1500
    await owner_says(sessionmaker, w.anna, last)
    kb = FakeKB(5, 600)
    system_len = len(prompts.system_prompt("dialog").text)

    full = await build_context(
        sessionmaker,
        KEYS,
        w.anna.user_id,
        w.anna.conv,
        query="x",
        knowledge=kb,
        budget=ContextBudget(max_chars=10**6),
    )
    assert full.stats.dropped == {} and not full.stats.over_budget

    budget = ContextBudget(max_chars=system_len + 12_000)
    built = await build_context(
        sessionmaker, KEYS, w.anna.user_id, w.anna.conv, query="x", knowledge=kb, budget=budget
    )
    s = built.stats
    assert s.chars <= budget.max_chars and not s.over_budget
    assert built.system.text == prompts.system_prompt("dialog").text
    assert built.dialog[-1].text == last and built.dialog[-1].author == "owner"
    # Порядок усечения: база знаний → сообщения пары → память → старые реплики.
    assert s.dropped["knowledge"] == 5 and s.dropped["pair_messages"] == 2
    assert s.dropped["memory"] == len(full.stats.memory_ids)
    assert 0 < s.dropped["dialog"] < len(full.dialog)
    kept = [t.text for t in built.dialog]
    assert kept == [t.text for t in full.dialog][-len(kept) :]  # выброшены самые старые

    tiny = await build_context(
        sessionmaker,
        KEYS,
        w.anna.user_id,
        w.anna.conv,
        query="x",
        knowledge=kb,
        budget=ContextBudget(max_chars=100),
    )
    assert tiny.stats.over_budget
    assert [t.text for t in tiny.dialog] == [last]  # последняя реплика не выбрасывается
    assert tiny.system.text == prompts.system_prompt("dialog").text
    assert tiny.context.memory == () and tiny.context.pair_messages == ()
    assert tiny.context.profile_summary == ""


async def test_budget_caps_single_items(sessionmaker: SM, engine: AsyncEngine) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег")
    await texts.add_memory_item(
        sessionmaker,
        KEYS,
        p.user_id,
        p.agent_id,
        "fact",
        "owner_dialog",
        "д" * 5000,
        fingerprints=TEST_FP,
    )
    await owner_says(sessionmaker, p, "о" * 5000)
    built = await build_context(
        sessionmaker,
        KEYS,
        p.user_id,
        p.conv,
        query="x",
        budget=ContextBudget(item_chars=100, turn_chars=300),
    )
    assert max(len(m) for m in built.context.memory) <= 100
    assert len(built.dialog[-1].text) <= 300 and built.dialog[-1].text.endswith("…")


# --------------------------------------------------------------------------- поток через БД


async def test_refusal_keeps_owner_message_and_crisis_records_type_only(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Порядок API: реплика владельца сохранена до вызова; при отказе модели она остаётся
    (§10); кризис фиксируется в ``safety_events`` только типом (§9.3)."""
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    body = "Мне страшно, он угрожал мне"
    mid = await owner_says(sessionmaker, p, body)
    built = await build_context(sessionmaker, KEYS, p.user_id, p.conv, query=body)
    refusing = ScriptedStreamProvider(
        lambda role, payload, system: {"category": "none", "secondary": []},
        stream=lambda payload: [LLMRefusal("отказ")],
    )
    events = [
        e
        async for e in ai_chat.stream_reply(
            AgentModels.single(refusing), built.context, built.dialog
        )
    ]
    assert events[-1].kind == "error"
    _, turns = await chat.read_history(sessionmaker, KEYS, p.user_id, p.conv)
    assert [(t.id, t.text) for t in turns] == [(mid, body)]

    crisis_p = ScriptedStreamProvider(
        lambda role, payload, system: {"category": "violence", "secondary": ["self_harm"]},
        stream=lambda payload: [LLMRefusal("отказ")],
    )
    sc = await ai_chat.screen(AgentModels.single(crisis_p), built.dialog)
    events = [
        e
        async for e in ai_chat.stream_reply(
            AgentModels.single(crisis_p), built.context, built.dialog, safety=sc
        )
    ]
    event = events[-1]
    assert isinstance(event, ai_chat.Crisis)
    assert await chat.record_safety_events(sessionmaker, p.user_id, event.safety_events) == 2
    assert await chat.add_message(sessionmaker, KEYS, p.user_id, p.conv, "agent", event.text)
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text("SELECT * FROM safety_events WHERE user_id = :u ORDER BY created_at, id"),
                    {"u": p.user_id},
                )
            )
            .mappings()
            .all()
        )
    assert sorted(r["type"] for r in rows) == ["self_harm", "violence"]
    assert all(set(r) == {"id", "user_id", "type", "created_at"} for r in rows)
    assert await chat.record_safety_events(sessionmaker, p.user_id, ["unknown"]) == 0


async def test_outgoing_threat_maps_to_db_event_type(sessionmaker: SM, engine: AsyncEngine) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    assert await chat.record_safety_events(sessionmaker, p.user_id, ["outgoing_threat"]) == 1
    async with engine.connect() as conn:
        value = (
            await conn.execute(
                text("SELECT type::text FROM safety_events WHERE user_id = :u"), {"u": p.user_id}
            )
        ).scalar_one()
    assert value == "threat_outgoing"


async def test_messages_stored_encrypted(sessionmaker: SM, engine: AsyncEngine) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    mid = await owner_says(sessionmaker, p, "секретная реплика")
    async with engine.connect() as conn:
        blob = (
            await conn.execute(
                text("SELECT text_enc FROM conversation_messages WHERE id = :m"), {"m": mid}
            )
        ).scalar_one()
    assert "секретная".encode() not in blob
    assert KEYS.open(crypto.CONVERSATION_MESSAGE_TEXT, mid, blob) == "секретная реплика"


async def test_current_conversation_is_stable(sessionmaker: SM, engine: AsyncEngine) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    assert await chat.current_conversation(sessionmaker, p.user_id) == p.conv


async def test_app_role_searches_memory_only_through_db_function(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """2.0v (A3), замена стража 2.1: прямого доступа приложения к функциям pgvector по-прежнему
    нет (``<=>``, приведение к ``vector``), поиск по смыслу — только функцией
    ``app_memory_nearest`` (АР-35), и она отдаёт только свою память."""
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    other = await person(engine, sessionmaker, "Олег", "Вера", chat_turns=False)
    for sql in (
        "SELECT embedding <=> embedding FROM memory_items WHERE id = :i",
        "SELECT CAST(CAST('[1,2]' AS text) AS vector) FROM memory_items WHERE id = :i",
    ):
        with pytest.raises(Exception, match="permission denied for function"):
            async with user_scope(sessionmaker, p.user_id) as s:
                await s.execute(text(sql), {"i": p.memory[0]})
    [vec] = await EMB.embed([other.mark("memory-money деньги отпуск")])
    async with user_scope(sessionmaker, p.user_id) as s:
        ids = [
            r.id
            for r in await s.execute(
                text("SELECT id FROM app_memory_nearest(:q, :m, 50)"),
                {"q": to_pgvector(vec), "m": EMB.model},
            )
        ]
    assert set(ids) == set(p.memory)  # чужая память не находится даже по её вектору

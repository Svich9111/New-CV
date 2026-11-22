"""2.1, этап 1 (A3), раунд 1 исправлений: ревью A6 (MAJOR-1, 2, 4; MINOR-1…7, 10, 11) и тест A8
(A8-1, О-1…О-3). Модель — заглушка, БД — тестовая; данные выдуманные."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ai import chat as ai_chat
from app.ai import dialog, summary
from app.ai.calls import as_json
from app.ai.context import MIN_TURN_CHARS, ContextBudget, build_context, size, turns_read_limit
from app.ai.dialog import OwnerContext, Turn
from app.ai.embeddings import HashingEmbedder
from app.ai.llm import FallbackProvider, LLMUnavailable, StructuredResult
from app.ai.models import AgentModels
from app.ai.prompts import system_prompt
from app.ai.stub import ScriptedProvider, ScriptedStreamProvider, json_chunks
from app.db import user_scope
from app.repositories import chat
from app.repositories.chat import HistorySummary, parse_vector, rank_by_similarity
from tests.test_account_delete import _couple, _send_encrypted
from tests.test_account_export_db import _deliver
from tests.test_chat_context import KEYS, P, owner_says, person
from tests.test_chat_stream import ANSWER, CTX, TURNS, SlowSafety, models, provider
from tests.test_couple_functions import SM

EMB = HashingEmbedder()


class Summarizer(ScriptedProvider):
    """Сжатие: ``fail`` первых вызовов — недоступность, дальше — сводка; запоминает пачки."""

    def __init__(self, fail: int = 0) -> None:
        super().__init__(lambda role, payload, system: {"summary": "s"})
        self.fail = fail
        self.batches: list[int] = []

    async def complete_json(self, **kw: Any) -> StructuredResult:
        payload = json.loads(kw["messages"][-1].content)
        if self.fail > 0:
            self.fail -= 1
            raise LLMUnavailable("нет сети")
        self.batches.append(len(payload["dialog"]))
        n = len(self.batches)
        return StructuredResult(
            data={"summary": f"сводка {n}"}, model="stub", usage=_usage(), latency_s=0, cost_usd=0
        )


def _usage() -> Any:
    from app.ai.llm import Usage

    return Usage()


async def add_turns(sm: SM, p: P, n: int, start: int = 0) -> list[uuid.UUID]:
    ids = []
    for i in range(start, start + n):
        author = "owner" if i % 2 == 0 else "agent"
        mid = await chat.add_message(sm, KEYS, p.user_id, p.conv, author, f"t{i}")
        assert mid is not None
        ids.append(mid)
    return ids


async def assert_no_gap(sm: SM, p: P, all_ids: list[uuid.UUID]) -> None:
    """Всё, что не в контексте, — в сводке: реплики контекста = все реплики после отметки,
    и вместе с покрытыми сводкой это ровно весь диалог (бюджет не ограничивает)."""
    s, after = await chat.read_history(sm, KEYS, p.user_id, p.conv)
    built = await build_context(
        sm, KEYS, p.user_id, p.conv, query="x", budget=ContextBudget(max_chars=10**6)
    )
    assert list(built.stats.turn_ids) == [t.id for t in after]
    covered = all_ids[: all_ids.index(s.through_id) + 1] if s and s.through_id else []
    assert covered + list(built.stats.turn_ids) == all_ids
    assert not built.stats.dropped


# ----------------------------------------------------------------- MAJOR-1: без разрыва


@pytest.mark.parametrize(
    ("trigger", "keep", "fail"),
    [(40, 12, 0), (40, 12, 10**6), (6, 2, 0), (6, 2, 3), (10, 9, 1), (3, 1, 0)],
)
async def test_no_gap_between_summary_and_context(
    sessionmaker: SM, engine: AsyncEngine, trigger: int, keep: int, fail: int
) -> None:
    """При любых порогах сжатия и сбоях модели сжатия контекст берёт все реплики после
    отметки сводки: разрыва (реплик ни в сводке, ни в контексте) нет. Прежний предел в 30
    реплик давал разрыв при 31…40 репликах после отметки."""
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    policy = summary.CompressionPolicy(trigger_turns=trigger, keep_turns=keep, batch_turns=5)
    bot = Summarizer(fail=fail)
    ids: list[uuid.UUID] = []
    for step in range(9):
        ids += await add_turns(
            sessionmaker, p, 9, start=len(ids)
        )  # нечётное: последняя — владельца
        await summary.compress_history(
            sessionmaker, KEYS, p.user_id, p.conv, AgentModels.single(bot), policy=policy
        )
        if step % 3 == 2 or step == 8:
            await assert_no_gap(sessionmaker, p, ids)
        ids += await add_turns(sessionmaker, p, 1, start=len(ids))  # ответ агента
    assert len(ids) == 90


async def test_long_tail_without_summary_all_turns_in_default_budget(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Сжатие не работает (нет кредитов): 61 короткая реплика — все в контексте по
    умолчанию (раньше — только 30)."""
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    ids = await add_turns(sessionmaker, p, 61)
    built = await build_context(sessionmaker, KEYS, p.user_id, p.conv, query="x")
    assert list(built.stats.turn_ids) == ids and not built.stats.dropped


def test_read_limit_cannot_cut_what_fits() -> None:
    """Реплика старше предела чтения не поместилась бы в бюджет даже самой короткой."""
    for max_chars in (0, 1, 500, 24_000, 100_000):
        n = turns_read_limit(max_chars)
        turns = [Turn("agent", "x")] * (n - 1) + [Turn("owner", "x")]
        assert size("", OwnerContext("", ""), turns) > max_chars
    assert MIN_TURN_CHARS > 10


# ----------------------------------------------------------------- MAJOR-2: пачка сжатия


async def test_compression_batches_catch_up_after_long_failures(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """После долгой серии сбоев (150 реплик без сводки) вход модели — не больше пачки,
    несколько раундов и вызовов догоняют хвост, разрыва нет."""
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    ids = await add_turns(sessionmaker, p, 151)
    policy = summary.CompressionPolicy(trigger_turns=20, keep_turns=6, batch_turns=40, rounds=2)
    bot = Summarizer(fail=4)
    for _ in range(4):  # сбои: сводки нет, вход модели не растёт
        r = await summary.compress_history(
            sessionmaker, KEYS, p.user_id, p.conv, AgentModels.single(bot), policy=policy
        )
        assert r.status == "error" and r.more
    first = await summary.compress_history(
        sessionmaker, KEYS, p.user_id, p.conv, AgentModels.single(bot), policy=policy
    )
    assert first.status == "saved" and first.rounds == 2 and first.summarized_turns == 80
    assert first.more
    second = await summary.compress_history(
        sessionmaker, KEYS, p.user_id, p.conv, AgentModels.single(bot), policy=policy
    )
    assert second.status == "saved" and not second.more
    assert max(bot.batches) <= 40
    await assert_no_gap(sessionmaker, p, ids)
    _, after = await chat.read_history(sessionmaker, KEYS, p.user_id, p.conv)
    assert len(after) <= policy.trigger_turns


def test_batch_respects_chars_and_takes_at_least_one() -> None:
    from datetime import UTC, datetime

    from app.repositories.texts import ConversationText

    now = datetime.now(UTC)
    turns = [ConversationText(uuid.uuid4(), "owner", "ж" * 900, now) for _ in range(10)]
    policy = summary.CompressionPolicy(trigger_turns=4, keep_turns=2, batch_chars=2000)
    assert len(summary.batch(turns, policy)) == 2
    tiny = summary.CompressionPolicy(trigger_turns=4, keep_turns=2, batch_chars=10)
    assert len(summary.batch(turns, tiny)) == 1


# ----------------------------------------------------------------- MAJOR-4, MINOR-6: поиск памяти


async def test_similarity_runs_off_event_loop(
    sessionmaker: SM, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег")
    await owner_says(sessionmaker, p, "деньги отпуск")
    seen: list[bool] = []
    real = chat.rank_by_similarity

    def spy(*args: Any) -> list[uuid.UUID]:
        seen.append(threading.current_thread() is threading.main_thread())
        return real(*args)

    monkeypatch.setattr(chat, "rank_by_similarity", spy)
    built = await build_context(sessionmaker, KEYS, p.user_id, p.conv, query="деньги", embedder=EMB)
    assert seen == [False] and built.stats.memory_search == "vector"


async def test_scan_limit_default_lowered() -> None:
    import inspect

    assert inspect.signature(chat.read_context).parameters["memory_scan_limit"].default == 200


class _Obj:
    def __init__(self, values: list[float], method: str) -> None:
        setattr(self, method, lambda: list(values))


@pytest.mark.parametrize(
    "raw",
    [
        "[1,0.5,-2]",
        [1.0, 0.5, -2.0],
        (1, 0.5, -2),
        _Obj([1, 0.5, -2], "to_list"),
        _Obj([1, 0.5, -2], "tolist"),
    ],
)
def test_parse_vector_driver_and_codec_types(raw: object) -> None:
    assert parse_vector(raw) == [1.0, 0.5, -2.0]


@pytest.mark.parametrize("raw", [None, 5, "1,2", "[a,b]", ["x"], b"[1]"])
def test_parse_vector_garbage_is_none(raw: object) -> None:
    assert parse_vector(raw) is None


def test_rank_skips_wrong_dimension_and_orders_ties_by_id() -> None:
    a, b, c = sorted(uuid.uuid4() for _ in range(3))
    rows: list[tuple[uuid.UUID, object]] = [(b, "[1,0]"), (a, "[1,0]"), (c, "[1,0,0]")]
    assert rank_by_similarity([1.0, 0.0], rows, 5) == [a, b]


async def test_parse_vector_reads_what_driver_returns(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    async with user_scope(sessionmaker, p.user_id) as s:
        raw = (
            await s.execute(
                text("SELECT embedding FROM memory_items WHERE id = :i"), {"i": p.memory[0]}
            )
        ).scalar_one()
    vec = parse_vector(raw)
    assert vec is not None and len(vec) == 1024


# ----------------------------------------------------------------- MINOR-7, MINOR-10: гонки


async def test_concurrent_first_requests_create_one_conversation(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    async with engine.begin() as conn:  # «новый» владелец: диалога ещё нет
        await conn.execute(text("DELETE FROM conversations WHERE id = :c"), {"c": p.conv})
    got = await asyncio.gather(
        *(chat.current_conversation(sessionmaker, p.user_id) for _ in range(6))
    )
    assert len(set(got)) == 1
    async with engine.connect() as conn:
        n = (
            await conn.execute(
                text("SELECT count(*) FROM conversations WHERE agent_id = :a"), {"a": p.agent_id}
            )
        ).scalar_one()
    assert n == 1


async def test_concurrent_summary_writes_one_wins(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Настоящая конкуренция: обе записи ждут отметку «нет сводки», FOR UPDATE пропускает одну."""
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    await add_turns(sessionmaker, p, 7)
    _, turns = await chat.read_history(sessionmaker, KEYS, p.user_id, p.conv)
    results = await asyncio.gather(
        *(
            chat.save_history_summary(
                sessionmaker,
                KEYS,
                p.user_id,
                p.conv,
                HistorySummary(f"сводка {i}", turns[i].created_at, turns[i].id),
                expected_through_id=None,
            )
            for i in range(5)
        )
    )
    assert sorted(results) == [False, False, False, False, True]
    s, _ = await chat.read_history(sessionmaker, KEYS, p.user_id, p.conv)
    winner = results.index(True)
    assert s is not None and s.text == f"сводка {winner}" and s.through_id == turns[winner].id


# ----------------------------------------------------------------- MINOR-11, О-3: лента пары


async def test_many_withheld_do_not_push_out_delivered(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Фильтр статуса и пары — в запросе до LIMIT: 120 более новых остановленных и ждущих
    сообщений не вытесняют доставленные (раньше окно — 100 строк ленты)."""
    a = await person(engine, sessionmaker, "Анна", "Игорь", chat_turns=False)
    b = await person(engine, sessionmaker, "Игорь", "Анна", chat_turns=False)
    couple, _ = await _couple(sessionmaker, a, b)
    _, delivered = await _send_encrypted(sessionmaker, KEYS, a, "доставленное своё")
    await _deliver(engine, KEYS, delivered, "доставленное своё", adapted=False)
    async with engine.begin() as conn:
        for i in range(120):
            await conn.execute(
                text(
                    "INSERT INTO partner_messages (couple_id, author_id, recipient_id, original_enc, "
                    "status, sent_at) VALUES (:c, :a, :r, :t, CAST(:s AS partner_message_status), "
                    "now() + make_interval(secs => :i))"
                ),
                {
                    "c": couple,
                    "a": a.user_id,
                    "r": b.user_id,
                    "t": b"x",
                    "s": "withheld" if i % 2 else "processing",
                    "i": i + 1,
                },
            )
    await owner_says(sessionmaker, a, "привет")
    built = await build_context(sessionmaker, KEYS, a.user_id, a.conv, query="привет")
    assert built.stats.pair_message_ids == (delivered,)
    assert built.payload()["pair_messages"] == [
        {"direction": "outgoing", "text": "доставленное своё"}
    ]


# ----------------------------------------------------------------- MINOR-1: отмена не теряется


async def test_cancel_helper_propagates_outer_cancellation() -> None:
    async def slow_to_cancel() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)
            raise

    inner = asyncio.create_task(slow_to_cancel())
    await asyncio.sleep(0)

    async def outer() -> str:
        await ai_chat._cancel(inner)
        return "дошли до конца"

    task = asyncio.create_task(outer())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.25)
    assert inner.cancelled()


async def test_cancel_helper_swallows_only_inner() -> None:
    async def forever() -> None:
        await asyncio.sleep(10)

    inner = asyncio.create_task(forever())
    await asyncio.sleep(0)
    await ai_chat._cancel(inner)  # своя отмена внутренней задачи — не ошибка
    assert inner.cancelled()


# ----------------------------------------------------------------- A8-1 / MINOR-2, MINOR-3


async def test_gate_releases_finished_answer_before_slow_verdict() -> None:
    """Агент дописал раньше придержки, классификатор медлит: ответ уходит по истечении
    придержки, ``Done`` — после вердикта."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    first = done = None
    async for e in ai_chat.stream_reply(
        models(provider(), SlowSafety(0.8)), CTX, TURNS, gate_timeout_s=0.15
    ):
        if isinstance(e, ai_chat.Delta) and first is None:
            first = loop.time() - start
        if isinstance(e, ai_chat.Done):
            done = loop.time() - start
    assert first is not None and done is not None
    assert first < 0.5 and done >= 0.75


async def test_gate_late_crisis_after_release_replaces() -> None:
    events = [
        e
        async for e in ai_chat.stream_reply(
            models(provider(), SlowSafety(0.4, "violence")), CTX, TURNS, gate_timeout_s=0.05
        )
    ]
    assert isinstance(events[0], ai_chat.Delta)
    assert isinstance(events[-1], ai_chat.Crisis) and events[-1].replaces_streamed


async def test_bad_output_not_released_by_gate() -> None:
    """Испорченный ответ не показывается кусками и после придержки — сразу ошибка."""
    p = provider(chunks=['{"level": 2, "reply": "Слышу тебя", "draft_ready": "да"}'])
    events = [
        e
        async for e in ai_chat.stream_reply(
            models(p, SlowSafety(0.3)), CTX, TURNS, gate_timeout_s=0.05
        )
    ]
    assert [e.kind for e in events] == ["error"]


async def test_stream_call_record_shows_fallback_model() -> None:
    primary = provider(chunks=[LLMUnavailable("503")])
    primary.model = "primary"
    backup = provider()
    backup.model = "backup"
    m = models(
        FallbackProvider(primary, backup),
        ScriptedProvider(lambda *a: {"category": "none", "secondary": []}),
    )
    events = [e async for e in ai_chat.stream_reply(m, CTX, TURNS)]
    done = events[-1]
    assert isinstance(done, ai_chat.Done)
    [rec] = [c for c in done.calls if c.role == "dialog"]
    assert rec.model == "backup" and rec.fallback_used is True
    plain = [e async for e in ai_chat.stream_reply(models(provider()), CTX, TURNS)]
    [rec2] = [c for c in plain[-1].calls if c.role == "dialog"]
    assert rec2.fallback_used is False and rec2.model == "stub"


# ----------------------------------------------------------------- MINOR-5


def test_ordered_payload_rejects_unknown_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    real = dialog.context_payload
    monkeypatch.setattr(dialog, "context_payload", lambda c, d: {**real(c, d), "lost_field": 1})
    with pytest.raises(ValueError, match="lost_field"):
        dialog.ordered_payload(CTX, TURNS)


# ----------------------------------------------------------------- О-1: ранняя ветка


async def test_screen_crisis_agent_never_started_even_if_consumer_awaits() -> None:
    """Значимость ранней ветки: API между событиями ждёт (запись в БД). Будь задача агента
    создана, она успела бы стартовать; ранняя ветка не создаёт её вовсе."""
    dialog_p = provider()
    m = models(dialog_p, ScriptedProvider(lambda *a: {"category": "self_harm", "secondary": []}))
    sc = await ai_chat.screen(m, TURNS)
    gen = ai_chat.stream_reply(m, CTX, TURNS, safety=sc)
    first = await gen.__anext__()
    assert isinstance(first, ai_chat.Crisis)
    await asyncio.sleep(0.05)  # «запись в БД» до следующего шага генератора
    assert dialog_p.stream_requests == []
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()
    assert dialog_p.stream_requests == []


# ----------------------------------------------------------------- О-2: вход классификатора


async def test_classifier_sees_last_three_owner_turns_despite_truncation(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    for i in range(3):
        await chat.add_message(
            sessionmaker, KEYS, p.user_id, p.conv, "owner", f"владелец {i} " + "в" * 3000
        )
    budget = ContextBudget(max_chars=len(system_prompt("dialog").text) + 4500)
    built = await build_context(sessionmaker, KEYS, p.user_id, p.conv, query="x", budget=budget)
    assert len(built.dialog) == 1 and built.stats.dropped.get("dialog") == 2
    assert [t.text[:10] for t in built.safety_dialog] == [f"владелец {i}"[:10] for i in range(3)]
    clf = ScriptedProvider(lambda *a: {"category": "none", "secondary": []})
    agent = ScriptedStreamProvider(clf.handler, stream=lambda payload: json_chunks(ANSWER))
    await ai_chat.screen(AgentModels(agent, agent, agent, clf, clf, clf), built.safety_dialog)
    seen = json.loads(clf.requests[-1]["messages"][0][1])["text"]
    assert all(f"владелец {i}" in seen for i in range(3))
    # параллельный режим берёт тот же вход
    events = [
        e
        async for e in ai_chat.stream_reply(
            AgentModels(agent, agent, agent, clf, clf, clf),
            built.context,
            built.dialog,
            safety_dialog=built.safety_dialog,
        )
    ]
    assert isinstance(events[-1], ai_chat.Done)
    seen = json.loads(clf.requests[-1]["messages"][0][1])["text"]
    assert all(f"владелец {i}" in seen for i in range(3))
    assert as_json(built.payload()).count("владелец") == 1

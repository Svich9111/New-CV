"""2.1 этап 1, A8, раунд 2: независимая проверка исправлений A3 (коммит 0d36172; АР-30, АР-31).

Помощники — только свои (``tests/test_chat_a8.py``), хелперы исполнителя не используются. Модель —
заглушки без сети, БД — тестовая (роль входа ``feelings_api`` → ``user_scope``), данные выдуманные.
Дефекты продукта — ``xfail(strict=True)`` (docs/reviews/2.1-test.md, «Раунд 2»).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import random
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.ai import chat as chat_ai
from app.ai.chat import ChatError, Crisis, Delta, Done, screen, stream_reply
from app.ai.context import ContextBudget, build_context, clip
from app.ai.embeddings import HashingEmbedder
from app.ai.llm import ChatMessage, LLMUnavailable, StructuredResult, Usage
from app.ai.prompts import system_prompt
from app.ai.summary import CompressionPolicy, compress_history
from app.core import crypto
from app.repositories import chat as chat_repo
from tests.test_chat_a8 import (
    CTX,
    DIALOG,
    FOREIGN,
    KEYS,
    OWN,
    QUERY,
    REPLY,
    FakeModel,
    World,
    _answer,
    _chunks,
    _conv,
    _draft,
    _mem,
    _ok_agent,
    _pmsg,
    _turn,
    _user,
    at,
    drain,
    models_of,
    world,  # noqa: F401 — фикстура
)

SYSTEM = system_prompt("dialog").text


# =============================================================== модель сжатия с цепочкой


@dataclass
class ChainSummarizer:
    """Сжатие: ответ — ``S<n>``; помнит вход каждого вызова; ``fail`` — план сбоев по вызовам."""

    fail: list[bool] = field(default_factory=list)
    model: str = "chain"
    inputs: dict[str, tuple[str, list[str]]] = field(default_factory=dict)
    n: int = 0

    async def complete_json(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        schema: Mapping[str, Any],
        max_tokens: int = 16000,
    ) -> StructuredResult:
        assert "summary" in schema["properties"]
        payload = json.loads(messages[-1].content)
        self.n += 1
        if self.fail and self.fail.pop(0):
            raise LLMUnavailable("сеть")
        name = f"S{self.n}"
        self.inputs[name] = (payload["previous_summary"], [t["text"] for t in payload["dialog"]])
        return StructuredResult(
            data={"summary": name}, model=self.model, usage=Usage(), latency_s=0, cost_usd=0
        )

    async def stream_text(self, **kw: Any) -> Any:  # pragma: no cover
        raise NotImplementedError
        yield ""


async def _all_turns(engine: AsyncEngine, conv: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    async with engine.connect() as c:
        rows = await c.execute(
            text(
                "SELECT id, text_enc FROM conversation_messages WHERE conversation_id = :c "
                "ORDER BY created_at, id"
            ),
            {"c": conv},
        )
        return [(r.id, KEYS.open(crypto.CONVERSATION_MESSAGE_TEXT, r.id, r.text_enc)) for r in rows]


async def _summary(engine: AsyncEngine, conv: uuid.UUID) -> dict[str, Any] | None:
    async with engine.connect() as c:
        blob = (
            await c.execute(
                text("SELECT summary_enc FROM conversations WHERE id = :c"), {"c": conv}
            )
        ).scalar_one()
    if blob is None:
        return None
    data: dict[str, Any] = json.loads(KEYS.open(crypto.CONVERSATION_SUMMARY, conv, bytes(blob)))
    return data


def _chain(summ: ChainSummarizer, name: str) -> list[str]:
    """Реплики, вошедшие в сводку ``name``, через цепочку прежних сводок, по порядку."""
    covered: list[str] = []
    while name:
        prev, inp = summ.inputs[name]
        covered = inp + covered
        name = prev
    return covered


# =============================================================== 1. Нет разрыва (перебор)

POLICIES = [
    dict(trigger_turns=t, keep_turns=k, batch_turns=b, batch_chars=bc, rounds=r)
    for t, k, b, bc, r in itertools.product((3, 6), (1, 2), (1, 3, 40), (300, 20_000), (1, 3))
    if t > k
]
FAILS: dict[str, Callable[[int], bool]] = {
    "never": lambda i: False,
    "every_other": lambda i: i % 2 == 1,
    "long_run": lambda i: 2 <= i < 12,
    "always": lambda i: True,
}
BUDGETS = {"default": 24_000, "tight": len(SYSTEM) + 1_400}
_rng = random.Random(20261122)  # noqa: S311 — не криптография
COMBOS = _rng.sample(list(itertools.product(range(len(POLICIES)), FAILS, BUDGETS)), 28)


@pytest.mark.parametrize(("pi", "fail", "budget"), COMBOS)
async def test_no_gap_between_summary_and_context_bruteforce(
    pi: int,
    fail: str,
    budget: str,
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """ТЗ §6.1 п.5 («последние реплики и краткое содержание предыдущих»), карточка 2.1 кр.3, ревью
    MAJOR-1/2: при любых порогах, пачках, сбоях модели сжатия и бюджете каждая реплика — ровно в
    одном месте: в цепочке сводок (без повторов, по порядку) или после отметки; из реплик после
    отметки в контекст идёт непрерывный хвост, а выброшенное бюджетом посчитано в
    ``stats.dropped["dialog"]``. Последняя реплика владельца — всегда."""
    policy = CompressionPolicy(**POLICIES[pi])
    rng = random.Random(f"{pi}{fail}{budget}")  # noqa: S311 — не криптография
    async with engine.begin() as c:
        uid, agent = await _user(c, name="Ника")
        conv = await _conv(c, agent)
    summ = ChainSummarizer(fail=[FAILS[fail](i) for i in range(200)])
    models = models_of(FakeModel(), summ=summ)  # type: ignore[arg-type]
    b = ContextBudget(max_chars=BUDGETS[budget])
    for step in range(22):
        author = "owner" if step % 2 == 0 else "agent"
        body = f"R{step:02d} " + "ж" * rng.choice((5, 60, 400, 900))
        async with engine.begin() as c:
            await _turn(c, conv, author, body, at(step) + timedelta(days=1))
        if author == "agent":
            await compress_history(sessionmaker, KEYS, uid, conv, models, policy=policy)
            continue
        everything = [t for _, t in await _all_turns(engine, conv)]
        env = await _summary(engine, conv)
        covered = _chain(summ, env["text"]) if env else []
        assert covered == everything[: len(covered)]  # цепочка сводок — начало истории подряд
        after = everything[len(covered) :]
        if env:
            ids = [i for i, _ in await _all_turns(engine, conv)]
            assert str(ids[len(covered) - 1]) == env["through_id"]  # отметка = последняя в сводке
        ctx = await build_context(sessionmaker, KEYS, uid, conv, query=body, budget=b)
        shown = [t.text for t in ctx.dialog]
        tail = [clip(t, b.turn_chars) for t in after]
        assert shown == tail[len(tail) - len(shown) :]  # непрерывный хвост
        assert len(tail) - len(shown) == ctx.stats.dropped.get("dialog", 0)
        assert shown[-1] == clip(body, b.turn_chars)
        if budget == "default" and not ctx.stats.over_budget:
            # Бюджет по умолчанию: ни одной реплики после отметки не выброшено, пока помещается.
            total = sum(len(t) for t in tail)
            if total < 15_000:
                assert shown == tail


async def test_batches_catch_up_after_long_failures(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Ревью MAJOR-2, ТЗ §10 (отказ модели): после долгой серии сбоев хвост догоняется пачками не
    больше ``batch_turns``/``batch_chars`` (одна огромная реплика — пачкой в одну), ``rounds`` за
    вызов, ``more`` — пока хвост длиннее порога; сводки идут цепочкой без пропусков."""
    policy = CompressionPolicy(trigger_turns=4, keep_turns=2, batch_turns=5, batch_chars=2_000,
                               rounds=3)  # fmt: skip
    async with engine.begin() as c:
        uid, agent = await _user(c, name="Ада")
        conv = await _conv(c, agent)
        for i in range(30):
            body = f"B{i:02d} " + ("ы" * 5_000 if i == 7 else "текст")
            await _turn(c, conv, "owner" if i % 2 == 0 else "agent", body, at(i))
    failing = ChainSummarizer(fail=[True] * 50)
    for _ in range(10):
        r = await compress_history(sessionmaker, KEYS, uid, conv,
                                   models_of(FakeModel(), summ=failing),  # type: ignore[arg-type]
                                   policy=policy)  # fmt: skip
        assert (r.status, r.more, r.rounds) == ("error", True, 0)
    assert await _summary(engine, conv) is None
    ok = ChainSummarizer()
    results = []
    for _ in range(10):
        r = await compress_history(sessionmaker, KEYS, uid, conv,
                                   models_of(FakeModel(), summ=ok),  # type: ignore[arg-type]
                                   policy=policy)  # fmt: skip
        results.append((r.status, r.rounds, r.more))
        if r.status == "skipped":
            break
    assert results[0] == ("saved", 3, True)
    assert results[-1][0] == "skipped"
    sizes = [len(inp) for _, inp in ok.inputs.values()]
    assert max(sizes) <= 5
    big = [inp for _, inp in ok.inputs.values() if any(t.startswith("B07") for t in inp)]
    assert big == [[next(t for t in big[0] if t.startswith("B07"))]]  # огромная — пачкой в одну
    env = await _summary(engine, conv)
    assert env is not None
    everything = [t for _, t in await _all_turns(engine, conv)]
    covered = _chain(ok, env["text"])
    assert covered == everything[: len(covered)] and len(everything) - len(covered) <= 4


async def test_read_limit_not_eaten_by_unreadable_rows(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """ТЗ §6.1 п.5, ревью MAJOR-1 («реплика вне предела чтения в контекст не вошла бы ни при каком
    усечении»): предел чтения считается по строкам, а нерасшифрованные строки места в бюджете не
    занимают. Если между старой короткой репликой и последней больше ``turns_read_limit``
    нерасшифрованных строк, старая реплика выпадает — её нет ни в сводке, ни в контексте, ни в
    ``dropped``, хотя она помещается."""
    b = ContextBudget(max_chars=len(SYSTEM) + 600)
    limit = (b.max_chars // 40) + 5
    async with engine.begin() as c:
        uid, agent = await _user(c, name="Юна")
        conv = await _conv(c, agent)
        await _turn(c, conv, "owner", "OLDFITSa8", at(0))
        for i in range(limit):
            await c.execute(
                text(
                    "INSERT INTO conversation_messages (id, conversation_id, author, text_enc, "
                    "created_at) VALUES (gen_random_uuid(), :c, 'agent', :t, :w)"
                ),
                {"c": conv, "t": b"\x00broken", "w": at(1 + i)},
            )
        await _turn(c, conv, "owner", "LASTa8", at(limit + 5))
    ctx = await build_context(sessionmaker, KEYS, uid, conv, query="LASTa8", budget=b)
    assert not ctx.stats.over_budget
    assert [t.text for t in ctx.dialog] == ["OLDFITSa8", "LASTa8"]


# =============================================================== 2. Придержка (A8-1)


async def test_gate_releases_valid_answer_at_gate_timeout() -> None:
    """A8-1 (ТЗ §10 «первый символ ≤ 5 с»): агент дописал раньше, классификатор медлит — проверенный
    ответ уходит по истечении ``gate_timeout_s`` (а не по вердикту); ``Done`` — после вердикта."""
    for gate, verdict in ((0.3, 1.5), (chat_ai.SAFETY_GATE_TIMEOUT_S, 4.5)):
        agent = FakeModel(stream=_chunks(_answer(REPLY)))
        clf = FakeModel(verdict_delay_s=verdict)
        events = await drain(stream_reply(models_of(agent, clf), CTX, DIALOG, gate_timeout_s=gate))
        first = next(t for t, e in events if isinstance(e, Delta))
        assert gate - 0.05 <= first <= gate + 0.3, (gate, first)
        assert "".join(e.text for _, e in events if isinstance(e, Delta)) == REPLY
        t_done, done = events[-1]
        assert isinstance(done, Done) and t_done >= verdict - 0.05


@pytest.mark.parametrize(
    "raw",
    [
        '{"level": 2, "reply": "Показанный кусок", "draft_ready": "да"}',
        '{"level": 7, "reply": "Показанный кусок", "draft_ready": false, "crisis_type": null, '
        '"crisis_secondary": []}',
        '{"reply": "Показанный кусок',
    ],
    ids=["bad_type", "bad_level", "truncated"],
)
async def test_gate_never_releases_broken_answer(raw: str) -> None:
    """ТЗ §10 (понятное сообщение), A8-1: испорченный ответ, дописанный в пределах придержки, не
    показывается ни по её истечении, ни после вердикта — только ``ChatError``; при кризисе —
    ``Crisis`` без показанного текста."""
    for verdict in (
        {"category": "none", "secondary": []},
        {"category": "self_harm", "secondary": []},
    ):
        agent = FakeModel(stream=_chunks(raw))
        clf = FakeModel(verdict=verdict, verdict_delay_s=0.6)
        events = [e for _, e in await drain(
            stream_reply(models_of(agent, clf), CTX, DIALOG, gate_timeout_s=0.1)
        )]  # fmt: skip
        assert not any(isinstance(e, Delta) for e in events)
        last = events[-1]
        if verdict["category"] == "none":
            assert isinstance(last, ChatError) and last.code == "bad_output"
        else:
            assert isinstance(last, Crisis) and last.replaces_streamed is False


async def test_gate_released_then_late_crisis_replaces() -> None:
    """ТЗ §9.3: ответ отпущен по сроку придержки, вердикт позже нашёл кризис — ``Crisis`` с
    заменой показанного, ``Done`` нет."""
    agent = FakeModel(stream=_chunks(_answer(REPLY)))
    clf = FakeModel(verdict={"category": "violence", "secondary": []}, verdict_delay_s=0.6)
    events = [e for _, e in await drain(
        stream_reply(models_of(agent, clf), CTX, DIALOG, gate_timeout_s=0.1)
    )]  # fmt: skip
    assert any(isinstance(e, Delta) for e in events)
    assert isinstance(events[-1], Crisis) and events[-1].replaces_streamed is True
    assert not any(isinstance(e, Done) for e in events)


# =============================================================== 3. Отмена внешней задачи


async def _consume(gen: Any, seen: list[Any]) -> None:
    async for ev in gen:
        seen.append(ev)


@pytest.mark.parametrize("phase", ["gate", "streaming"])
async def test_outer_cancel_propagates(phase: str) -> None:
    """Ревью MINOR-1 (отмена внешней задачи не теряется): клиент ушёл — задача-потребитель
    отменена; отмена доходит до вызывающего (задача ``cancelled()``), а не глушится."""
    agent = FakeModel(stream=[*_chunks(_answer(REPLY))[:6], *["x"] * 50], chunk_delay_s=0.3)
    clf = FakeModel(verdict_delay_s=30 if phase == "gate" else 0.0)
    seen: list[Any] = []
    task = asyncio.create_task(
        _consume(stream_reply(models_of(agent, clf), CTX, DIALOG, gate_timeout_s=10), seen)
    )
    await asyncio.sleep(0.8)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.mark.parametrize("phase", ["gate", "streaming"])
async def test_outer_cancel_also_cancels_model_calls(phase: str) -> None:
    """Карточка 2.1 «закрытие потока отменяет вызовы», ТЗ §10 (учёт расходов): при отмене задачи-
    потребителя (обрыв SSE) вызовы агента и классификатора должны быть отменены. Сейчас в
    ``finally`` первый ``_cancel(get_task)`` пробрасывает отмену текущей задачи, и
    ``_cancel(agent_task)``/``_cancel(safety_task)`` не выполняются — вызовы модели продолжаются."""
    agent = FakeModel(stream=[*_chunks(_answer(REPLY))[:6], *["x"] * 50], chunk_delay_s=0.3)
    clf = FakeModel(verdict_delay_s=30 if phase == "gate" else 5.0)
    seen: list[Any] = []
    task = asyncio.create_task(
        _consume(stream_reply(models_of(agent, clf), CTX, DIALOG, gate_timeout_s=10), seen)
    )
    await asyncio.sleep(0.8)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.1)
    assert agent.stream_cancelled and agent.stream_closed
    assert clf.verdict_cancelled or clf.verdict_done_at is not None


# =============================================================== 4. Конкуренция в БД


async def test_concurrent_first_requests_one_conversation(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Ревью MINOR-7: одновременные первые запросы владельца — один диалог (20 владельцев × 8
    одновременных запросов, чтобы гонка проявлялась без блокировки)."""
    owners = []
    async with engine.begin() as c:
        for i in range(20):
            owners.append(await _user(c, name=f"Ося{i}"))
    for uid, agent in owners:
        ids = await asyncio.gather(
            *(chat_repo.current_conversation(sessionmaker, uid) for _ in range(8))
        )
        assert len(set(ids)) == 1
        async with engine.connect() as c:
            n = (
                await c.execute(
                    text("SELECT count(*) FROM conversations WHERE agent_id = :a"), {"a": agent}
                )
            ).scalar_one()
        assert n == 1


async def test_long_tail_after_mark_fully_in_default_budget(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """ТЗ §6.1 п.5, ревью MAJOR-1: сжатие долго не работало, после отметки 80 коротких реплик —
    в бюджет по умолчанию они помещаются и все идут в контекст (прежний предел 30 их отсекал)."""
    async with engine.begin() as c:
        uid, agent = await _user(c, name="Тая")
        conv = await _conv(c, agent)
        for i in range(80):
            await _turn(c, conv, "owner" if i % 2 else "agent", f"S{i:02d}", at(i))
    ctx = await build_context(sessionmaker, KEYS, uid, conv, query="S79")
    assert [t.text for t in ctx.dialog] == [f"S{i:02d}" for i in range(80)]
    assert ctx.stats.dropped.get("dialog", 0) == 0


async def test_concurrent_summary_writes_exactly_one_wins(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Карточка 2.1 «одновременная запись — конфликт без потери»: N одновременных записей с одной
    ожидаемой отметкой — ровно одна успешна, в БД — её сводка."""
    async with engine.begin() as c:
        uid, agent = await _user(c, name="Ира")
        conv = await _conv(c, agent)
        tids = [await _turn(c, conv, "owner", f"t{i}", at(i)) for i in range(12)]
    for _round in range(2):
        env = await _summary(engine, conv)
        exp = uuid.UUID(env["through_id"]) if env else None
        res = await asyncio.gather(*(
            chat_repo.save_history_summary(
                sessionmaker, KEYS, uid, conv,
                chat_repo.HistorySummary(f"W{_round}{i}", at(1 + 5 * _round + i), tids[1 + 5 * _round + i]),
                expected_through_id=exp,
            )
            for i in range(5)
        ))  # fmt: skip
        assert sum(res) == 1, (_round, res, exp)
        win = res.index(True)
        env2 = await _summary(engine, conv)
        assert env2 is not None and env2["text"] == f"W{_round}{win}"


# =============================================================== 5. Лента пары через черновики


async def test_pair_via_drafts_isolation(
    world: World,  # noqa: F811
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """ТЗ §6.1 п.6, §9.2 п.1, АР-11, ревью MINOR-11: блок пары читается из ``partner_messages`` и
    своих отправленных черновиков. Не попадают: чужие черновики (и отправленные партнёром —
    их оригиналы), свои неотправленные и одобренные черновики, бывшая пара; 120 более новых своих
    ``processing``/``withheld`` не вытесняют доставленное (О-3)."""
    async with engine.begin() as c:
        await _draft(c, world.anna_agent, "OWNUNSENTa8 ссора")
        await _draft(c, world.anna_agent, "OWNAPPROVEDa8 ссора", "approved")
        anna_ag = (world.anna, world.anna_agent)
        for i in range(120):
            await _pmsg(c, world.couple, anna_ag, world.igor, f"STUCK{i}a8",
                        "withheld" if i % 2 else "processing", at(1000 + i))  # fmt: skip
    ctx = await build_context(sessionmaker, KEYS, world.anna, world.conv, query=QUERY,
                              embedder=HashingEmbedder())  # fmt: skip
    pair = [m.text for m in ctx.context.pair_messages]
    assert pair == [OWN["out_original"], OWN["in_adapted"], OWN["in_plain"]]
    sent = "\n".join(pair)
    for mark in ("OWNUNSENTa8", "OWNAPPROVEDa8", "STUCK", FOREIGN["igor_draft"],
                 FOREIGN["igor_draft_approved"], FOREIGN["igor_in_original"], FOREIGN["ex_in"],
                 FOREIGN["ex_in_adapted"], FOREIGN["ex_out"], FOREIGN["out_adapted_for_igor"]):  # fmt: skip
        assert mark not in sent


async def test_pair_incoming_never_reads_partner_draft(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """АР-11, §9.2 п.1: даже если у полученного сообщения нет версии для получателя, оригинал из
    черновика партнёра (его данные) не подставляется — сообщение просто не входит в контекст."""
    async with engine.begin() as c:
        a, a_ag = await _user(c, name="Лия")
        b, b_ag = await _user(c, name="Марк")
        conv = await _conv(c, a_ag)
        await _turn(c, conv, "owner", "привет", at(1))
        k = uuid.uuid4()
        await c.execute(text("INSERT INTO couples (id, status) VALUES (:k, 'active')"), {"k": k})
        await c.execute(
            text(
                "INSERT INTO couple_members (couple_id, user_id, role) VALUES "
                "(:k, :a, 'initiator'), (:k, :b, 'partner')"
            ),
            {"k": k, "a": a, "b": b},
        )
        mid = await _pmsg(c, k, (b, b_ag), a, "MARKDRAFTa8", "delivered", at(2),
                          for_recipient="FORLIAa8")  # fmt: skip
        # Версия для получателя потеряна (как после сбоя): adapted_enc NULL.
        await c.execute(
            text("UPDATE partner_messages SET adapted_enc = NULL WHERE id = :m"), {"m": mid}
        )
    ctx = await build_context(sessionmaker, KEYS, a, conv, query="привет")
    assert ctx.context.pair_messages == ()
    assert "MARKDRAFTa8" not in json.dumps(ctx.payload(), ensure_ascii=False)


# =============================================================== 6. safety_dialog при усечении


async def test_classifier_sees_three_owner_turns_despite_truncation(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Тест О-2, ТЗ §9.3: бюджет выбросил старые реплики владельца из диалога агента, но вторая
    линия видит три последние реплики владельца (``safety_dialog``) — и в ``screen``, и в
    параллельном режиме; агенту — только то, что поместилось."""
    async with engine.begin() as c:
        uid, agent = await _user(c, name="Зоя")
        conv = await _conv(c, agent)
        bodies = [f"OWN{i}a8 " + "у" * 1_500 for i in range(5)]
        for i, body in enumerate(bodies):
            await _turn(c, conv, "owner", body, at(2 * i))
            if i < 4:
                await _turn(c, conv, "agent", f"AG{i}a8", at(2 * i + 1))
    b = ContextBudget(max_chars=len(SYSTEM) + 2_200)
    ctx = await build_context(sessionmaker, KEYS, uid, conv, query=bodies[-1], budget=b)
    assert [t.text for t in ctx.dialog if t.author == "owner"] == [bodies[-1]]
    assert [t.text for t in ctx.safety_dialog] == bodies[-3:]
    clf = FakeModel()
    sc = await screen(models_of(FakeModel(), clf), ctx.safety_dialog)
    assert sc.error is None
    clf2 = FakeModel()
    await drain(stream_reply(models_of(_ok_agent(), clf2), ctx.context, ctx.dialog,
                             safety_dialog=ctx.safety_dialog))  # fmt: skip
    for model in (clf, clf2):
        [call] = model.kinds("safety")
        seen = json.loads(call["messages"][0][1])["text"]
        assert all(body in seen for body in bodies[-3:])
        assert bodies[0] not in seen and "AG" not in seen


# =============================================================== 7. Косинус вне цикла событий


async def test_similarity_off_event_loop_and_scan_limit(
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ревью MAJOR-4 (Б-55): косинус по векторам памяти считается не в потоке цикла событий и не
    больше чем по 200 свежим векторам; цикл событий в это время отвечает."""
    emb = HashingEmbedder()
    async with engine.begin() as c:
        uid, agent = await _user(c, name="Ян")
        conv = await _conv(c, agent)
        await _turn(c, conv, "owner", "деньги ссора", at(0))
        vecs = await emb.embed([f"запись {i} деньги" for i in range(230)])
        for i, v in enumerate(vecs):
            await _mem(c, agent, f"M{i}", at(10 + i), vector=v)
    loop_thread = threading.get_ident()
    calls: list[tuple[int, int]] = []
    real = chat_repo.rank_by_similarity

    def spy(query: Sequence[float], rows: Sequence[tuple[uuid.UUID, object]], limit: int) -> Any:
        calls.append((threading.get_ident(), len(rows)))
        return real(query, rows, limit)

    monkeypatch.setattr(chat_repo, "rank_by_similarity", spy)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    t = asyncio.create_task(ticker())
    ctx = await build_context(sessionmaker, KEYS, uid, conv, query="деньги ссора", embedder=emb)
    t.cancel()
    assert calls and all(th != loop_thread for th, _ in calls)
    assert all(n <= 200 for _, n in calls)
    assert ctx.stats.memory_search == "vector" and len(ctx.context.memory) == 8
    assert ticks > 0

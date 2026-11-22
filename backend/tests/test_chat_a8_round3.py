"""2.1 этап 1, A8, раунд 3: независимая проверка исправлений A3 (коммит 3b89a9e).

Помощники — только свои (``tests/test_chat_a8*.py``). Модель — заглушки без сети, БД — тестовая
(роль входа ``feelings_api`` → ``user_scope``), данные выдуманные. Дефекты продукта —
``xfail(strict=True)`` (docs/reviews/2.1-test.md, «Раунд 3»)."""

from __future__ import annotations

import asyncio
import itertools
import json
import random
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.ai.chat import Delta, screen, stream_reply
from app.ai.context import ContextBudget, build_context, clip, turns_read_limit
from app.ai.dialog import ContextDialog
from app.ai.llm import ChatMessage, StructuredResult
from app.ai.prompts import system_prompt
from app.ai.summary import CompressionPolicy, compress_history, read_limit
from app.core import crypto
from app.repositories import chat as chat_repo
from app.repositories import texts as texts_repo
from tests.test_chat_a8 import (
    CTX,
    DIALOG,
    KEYS,
    REPLY,
    FakeModel,
    _answer,
    _chunks,
    _conv,
    _ok_agent,
    _turn,
    _user,
    at,
    drain,
    models_of,
)
from tests.test_chat_a8_round2 import ChainSummarizer, _chain, _summary

SYSTEM = system_prompt("dialog").text


# =============================================================== 1. Отмена в каждой точке


@dataclass
class SlowCancelModel(FakeModel):
    """Как ``FakeModel``, но на отмену отвечает не сразу (``cancel_delay_s``): вторая отмена
    приходит, пока поток ждёт свои задачи в ``finally``."""

    cancel_delay_s: float = 0.0
    stream_live: bool = False
    verdict_live: bool = False

    async def complete_json(self, **kw: Any) -> StructuredResult:
        self.verdict_live = True
        try:
            return await super().complete_json(**kw)
        except asyncio.CancelledError:
            if self.cancel_delay_s:
                await asyncio.sleep(self.cancel_delay_s)
            raise
        finally:
            self.verdict_live = False

    async def stream_text(
        self, *, system: str, messages: Sequence[ChatMessage], max_tokens: int = 16000
    ) -> AsyncIterator[str]:
        self.stream_live = True
        try:
            async for chunk in super().stream_text(
                system=system, messages=messages, max_tokens=max_tokens
            ):
                yield chunk
        except asyncio.CancelledError:
            if self.cancel_delay_s:
                await asyncio.sleep(self.cancel_delay_s)
            raise
        finally:
            self.stream_live = False


LONG = [*_chunks(_answer(REPLY))[:6], *["x"] * 60]


@dataclass
class Point:
    """Сценарий: модели, параметры ``stream_reply`` и момент отмены."""

    agent: SlowCancelModel
    clf: SlowCancelModel
    kwargs: dict[str, Any] = field(default_factory=dict)
    cancel_after_s: float = 0.9
    use_screen: bool = False
    expect_stream: bool = True  # поток агента был начат и должен быть отменён
    expect_verdict: bool = True  # классификатор ждал и должен быть отменён


def _points(delay: float = 0.0) -> dict[str, Callable[[], Point]]:
    def m(**kw: Any) -> SlowCancelModel:
        return SlowCancelModel(cancel_delay_s=delay, **kw)

    return {
        # агент ещё не дал ни куска, классификатор медлит
        "before_first_chunk": lambda: Point(
            m(stream=_chunks(_answer(REPLY)), first_delay_s=5), m(verdict_delay_s=5)
        ),
        # куски пришли, но придержаны до вердикта
        "during_gate": lambda: Point(
            m(stream=LONG, chunk_delay_s=0.05), m(verdict_delay_s=5), {"gate_timeout_s": 10}
        ),
        # придержка открыта, куски идут
        "streaming": lambda: Point(
            m(stream=LONG, chunk_delay_s=0.1), m(verdict_delay_s=5), {"gate_timeout_s": 0.05}
        ),
        # агент дописал, ответ отпущен по сроку, ждём вердикт до Done
        "awaiting_verdict": lambda: Point(
            m(stream=_chunks(_answer(REPLY))),
            m(verdict_delay_s=5),
            {"gate_timeout_s": 0.05},
            expect_stream=False,
        ),
        # режим АР-30: вердикт уже есть (screen), идёт только агент
        "screened_streaming": lambda: Point(
            m(stream=LONG, chunk_delay_s=0.1), m(), use_screen=True, expect_verdict=False
        ),
    }


async def _run_point(p: Point, *, double: bool) -> tuple[asyncio.Task[Any], list[Any]]:
    before = asyncio.all_tasks()
    models = models_of(p.agent, p.clf)
    kwargs = dict(p.kwargs)
    if p.use_screen:
        kwargs["safety"] = await screen(models, DIALOG)
    seen: list[Any] = []

    async def consume() -> None:
        async for ev in stream_reply(models, CTX, DIALOG, **kwargs):
            seen.append(ev)

    task = asyncio.create_task(consume())
    await asyncio.sleep(p.cancel_after_s)
    task.cancel()
    if double:
        await asyncio.sleep(0.02)  # первая отмена дошла, поток ждёт задачи в finally
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    leftover = {t for t in asyncio.all_tasks() - before if not t.done()}
    leftover.discard(asyncio.current_task())
    assert leftover == set(), leftover
    return task, seen


@pytest.mark.parametrize("double", [False, True], ids=["single", "double"])
@pytest.mark.parametrize("point", list(_points()))
async def test_outer_cancel_at_every_point(point: str, double: bool) -> None:
    """Карточка 2.1 «закрытие потока отменяет вызовы», ТЗ §10 (расходы), ревью R2-MAJOR / A8-2:
    отмена задачи-потребителя в каждой точке потока (до первого куска, в придержке, в потоке, в
    ожидании вердикта, в режиме «сначала классификатор»), одна или две подряд — отмена доходит до
    вызывающего, незавершённых задач не остаётся, начатые вызовы модели отменены."""
    p = _points(delay=0.1 if double else 0.0)[point]()
    task, seen = await _run_point(p, double=double)
    assert task.cancelled()
    assert not p.agent.stream_live and not p.clf.verdict_live
    if p.expect_stream:
        assert p.agent.stream_cancelled, "поток агента не отменён"
    if p.expect_verdict:
        assert p.clf.verdict_cancelled, "классификатор не отменён"
    if point == "during_gate":
        assert not any(isinstance(e, Delta) for e in seen)
    if point in ("streaming", "awaiting_verdict", "screened_streaming"):
        assert any(isinstance(e, Delta) for e in seen)


async def test_aclose_and_break_still_cancel_everything() -> None:
    """Закрытие генератора (``aclose`` / выход из ``async for``) — как в раунде 1, после замены
    ``_cancel`` на ``_cancel_all``: вызовы отменены, задач не остаётся."""
    before = asyncio.all_tasks()
    agent = SlowCancelModel(stream=LONG, chunk_delay_s=0.1, cancel_delay_s=0.05)
    clf = SlowCancelModel(verdict_delay_s=5, cancel_delay_s=0.05)
    gen = stream_reply(models_of(agent, clf), CTX, DIALOG, gate_timeout_s=0.05)
    async for ev in gen:
        if isinstance(ev, Delta):
            break
    await gen.aclose()  # type: ignore[attr-defined]
    assert agent.stream_cancelled and clf.verdict_cancelled
    assert {t for t in asyncio.all_tasks() - before if not t.done()} == set()


async def test_cancel_during_aclose_cleanup_is_not_lost() -> None:
    """Ревью MINOR-1/R2-MAJOR: клиент закрыл поток (``aclose``), а пока поток ждёт отмены своих
    вызовов, отменили и саму задачу запроса — эта отмена не теряется (задача ``cancelled()``)."""
    agent = SlowCancelModel(stream=LONG, chunk_delay_s=0.1, cancel_delay_s=0.4)
    clf = SlowCancelModel(verdict_delay_s=5, cancel_delay_s=0.4)
    after_close: list[str] = []

    async def consume() -> None:
        gen = stream_reply(models_of(agent, clf), CTX, DIALOG, gate_timeout_s=0.05)
        async for ev in gen:
            if isinstance(ev, Delta):
                break
        await gen.aclose()  # type: ignore[attr-defined]
        after_close.append("продолжил после отмены")
        await asyncio.sleep(5)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.6)  # первый кусок пришёл, идёт aclose → ожидание отмены вызовов
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled() and after_close == []
    assert agent.stream_cancelled and clf.verdict_cancelled


# =============================================================== 2. Нечитаемые строки


async def _broken_rows(engine: AsyncEngine, conv: uuid.UUID, n: int, start: int) -> None:
    async with engine.begin() as c:
        await c.execute(
            text(
                "INSERT INTO conversation_messages (id, conversation_id, author, text_enc, "
                "created_at) SELECT gen_random_uuid(), :c, 'agent', :b, "
                "CAST(:t0 AS timestamptz) + make_interval(secs => g) FROM generate_series(1, :n) AS g"
            ),
            {"c": conv, "n": n, "t0": at(start), "b": b"\x00broken"},
        )


async def test_unreadable_rows_skipped_and_counted(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A8-3, ТЗ §6.1 п.5: нечитаемые строки предел чтения не съедают — старая реплика, которая
    помещается, в контексте; число нечитаемых — в ``stats.unavailable["dialog"]``."""
    b = ContextBudget(max_chars=len(SYSTEM) + 600)
    n = turns_read_limit(b.max_chars) * 3
    async with engine.begin() as c:
        uid, agent = await _user(c, name="Рая")
        conv = await _conv(c, agent)
        await _turn(c, conv, "owner", "OLDa8", at(0))
    await _broken_rows(engine, conv, n, 1)
    async with engine.begin() as c:
        await _turn(c, conv, "owner", "LASTa8", at(1) + timedelta(seconds=n + 10))
    ctx = await build_context(sessionmaker, KEYS, uid, conv, query="LASTa8", budget=b)
    assert [t.text for t in ctx.dialog] == ["OLDa8", "LASTa8"]
    assert ctx.stats.unavailable.get("dialog") == n


async def test_scan_ceiling_loss_is_reported(
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A8-3, потолок просмотра (``TURN_SCAN_FACTOR × limit``): за потолком старая реплика в
    контекст не идёт, но потеря не молчаливая — в ``stats.unavailable["dialog"]`` (больше
    просмотренных нечитаемых), сборка не падает и укладывается в бюджет."""
    b = ContextBudget(max_chars=len(SYSTEM) + 600)
    limit = turns_read_limit(b.max_chars)
    n = (chat_repo.TURN_SCAN_FACTOR + 2) * limit
    async with engine.begin() as c:
        uid, agent = await _user(c, name="Ева")
        conv = await _conv(c, agent)
        await _turn(c, conv, "owner", "BEYONDa8", at(0))
    await _broken_rows(engine, conv, n, 1)
    async with engine.begin() as c:
        await _turn(c, conv, "owner", "LASTa8", at(1) + timedelta(seconds=n + 10))
    count = {"n": 0}
    real = texts_repo.open_row

    def spy(keys: Any, fld: crypto.Field, row_id: Any, blob: bytes) -> Any:
        if fld == crypto.CONVERSATION_MESSAGE_TEXT:
            count["n"] += 1
        return real(keys, fld, row_id, blob)

    monkeypatch.setattr(chat_repo, "open_row", spy)
    ctx = await build_context(sessionmaker, KEYS, uid, conv, query="LASTa8", budget=b)
    assert [t.text for t in ctx.dialog] == ["LASTa8"]
    assert count["n"] < n  # потолок сработал: просмотрены не все строки
    # Потери: все просмотренные нечитаемые (count − 1 читаемая) + 1 — «дальше не смотрели».
    assert ctx.stats.unavailable.get("dialog") == count["n"]
    assert not ctx.stats.over_budget and ctx.stats.chars <= b.max_chars


# =============================================================== 3. screen: три вида входа


async def test_screen_accepts_built_context_dialog_and_plain_list(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Ревью R2-MINOR-1, ТЗ §9.3: ``screen(models, built)`` и ``screen(models, built.dialog)``
    дают классификатору три последние реплики владельца до усечения; обычный список — как есть
    (документированное поведение); параллельный режим с ``built.dialog`` — тоже до усечения."""
    async with engine.begin() as c:
        uid, agent = await _user(c, name="Ната")
        conv = await _conv(c, agent)
        bodies = [f"OWN{i}a8 " + "у" * 1_500 for i in range(5)]
        for i, body in enumerate(bodies):
            await _turn(c, conv, "owner", body, at(2 * i))
            if i < 4:
                await _turn(c, conv, "agent", f"AG{i}a8", at(2 * i + 1))
    b = ContextBudget(max_chars=len(SYSTEM) + 2_200)
    built = await build_context(sessionmaker, KEYS, uid, conv, query=bodies[-1], budget=b)
    assert isinstance(built.dialog, ContextDialog)
    assert [t.text for t in built.dialog if t.author == "owner"] == [bodies[-1]]

    def seen_by(clf: FakeModel) -> str:
        [call] = clf.kinds("safety")
        return str(json.loads(call["messages"][0][1])["text"])

    for source in (built, built.dialog):
        clf = FakeModel()
        await screen(models_of(FakeModel(), clf), source)
        assert seen_by(clf) == "\n".join(bodies[-3:])
    clf = FakeModel()
    await screen(models_of(FakeModel(), clf), list(built.dialog))
    assert seen_by(clf) == bodies[-1]
    clf = FakeModel()
    await drain(stream_reply(models_of(_ok_agent(), clf), built.context, built.dialog))
    assert seen_by(clf) == "\n".join(bodies[-3:])


@pytest.mark.parametrize(
    "derive",
    [lambda d: d[-5:], lambda d: tuple(d), lambda d: (*d,)],
    ids=["slice", "tuple", "unpack"],
)
async def test_derived_dialog_loses_safety_view(
    derive: Callable[[ContextDialog], Any],
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """О-4 (к сведению, этап 2): ``ContextDialog`` — подкласс ``tuple``; срез, ``tuple(...)``,
    распаковка дают обычный кортеж без ``safety`` — классификатор тогда видит усечённый диалог.
    Фиксирует поведение: API должен передавать ``built`` или ``built.dialog`` без преобразований."""
    async with engine.begin() as c:
        uid, agent = await _user(c, name="Влада")
        conv = await _conv(c, agent)
        for i in range(4):
            await _turn(c, conv, "owner", f"P{i}a8 " + "о" * 1_500, at(i))
    b = ContextBudget(max_chars=len(SYSTEM) + 1_800)
    built = await build_context(sessionmaker, KEYS, uid, conv, query="x", budget=b)
    derived = derive(built.dialog)
    assert not isinstance(derived, ContextDialog)
    clf = FakeModel()
    await screen(models_of(FakeModel(), clf), derived)
    [call] = clf.kinds("safety")
    assert "P1a8" not in json.loads(call["messages"][0][1])["text"]


# =============================================================== 4. Сжатие: только голова


async def test_compression_decrypts_only_head(
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ревью R2-MINOR-3: раунд сжатия расшифровывает только первые ``read_limit(policy)`` реплик
    после отметки (а не весь хвост из 600), а за вызов — не больше ``(rounds + 1) × read_limit``."""
    policy = CompressionPolicy()
    async with engine.begin() as c:
        uid, agent = await _user(c, name="Гая")
        conv = await _conv(c, agent)
        await c.execute(
            text(
                "INSERT INTO conversation_messages (id, conversation_id, author, text_enc, "
                "created_at) VALUES (:i, :c, CAST(:a AS message_author), :t, :w)"
            ),
            [
                {
                    "i": (i_ := uuid.uuid4()),
                    "c": conv,
                    "a": "owner" if i % 2 else "agent",
                    "t": KEYS.seal(crypto.CONVERSATION_MESSAGE_TEXT, i_, f"H{i:03d}"),
                    "w": at(i),
                }
                for i in range(600)
            ],
        )
    count = {"n": 0}
    real = texts_repo.open_row

    def spy(keys: Any, fld: crypto.Field, row_id: Any, blob: bytes) -> Any:
        if fld == crypto.CONVERSATION_MESSAGE_TEXT:
            count["n"] += 1
        return real(keys, fld, row_id, blob)

    monkeypatch.setattr(chat_repo, "open_row", spy)
    summ = ChainSummarizer()
    r = await compress_history(sessionmaker, KEYS, uid, conv,
                               models_of(FakeModel(), summ=summ),  # type: ignore[arg-type]
                               policy=policy)  # fmt: skip
    assert (r.status, r.rounds, r.more) == ("saved", policy.rounds, True)
    head = max(policy.trigger_turns, policy.batch_turns)  # не через read_limit: сверка с ТЗ ревью
    assert read_limit(policy) == head
    assert count["n"] <= (policy.rounds + 1) * head
    assert all(len(inp) <= policy.batch_turns for _, inp in summ.inputs.values())


POLICIES = [
    dict(trigger_turns=t, keep_turns=k, batch_turns=b, batch_chars=bc, rounds=r)
    for t, k, b, bc, r in itertools.product((3, 8), (1, 2), (1, 4, 40), (250, 20_000), (1, 2))
    if t > k
]
_rng = random.Random(3)  # noqa: S311 — не криптография
COMBOS = _rng.sample(list(itertools.product(range(len(POLICIES)), (0, 15, 35))), 16)


@pytest.mark.parametrize(("pi", "fail_first"), COMBOS)
async def test_head_reading_catches_up_without_gap(
    pi: int,
    fail_first: int,
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """ТЗ §6.1 п.5, R2-MINOR-3: сжатие по голове хвоста после серии из ``fail_first`` сбоев
    догоняет хвост; на каждом шаге цепочка сводок — начало истории подряд, отметка — её последняя
    реплика, в контексте — непрерывный хвост после отметки (в т.ч. с нечитаемыми строками: они
    пропускаются, читаемые не теряются)."""
    policy = CompressionPolicy(**POLICIES[pi])
    rng = random.Random(pi * 100 + fail_first)  # noqa: S311 — не криптография
    async with engine.begin() as c:
        uid, agent = await _user(c, name="Мила")
        conv = await _conv(c, agent)
    summ = ChainSummarizer(fail=[i < fail_first for i in range(500)])
    models = models_of(FakeModel(), summ=summ)  # type: ignore[arg-type]
    broken: set[uuid.UUID] = set()
    for step in range(50):
        author = "owner" if step % 2 == 0 else "agent"
        body = f"Q{step:02d} " + "в" * rng.choice((3, 50, 700))
        async with engine.begin() as c:
            tid = await _turn(c, conv, author, body, at(step) + timedelta(days=3))
            if author == "agent" and rng.random() < 0.15:
                await c.execute(
                    text("UPDATE conversation_messages SET text_enc = :b WHERE id = :i"),
                    {"i": tid, "b": b"\x00x"},
                )
                broken.add(tid)
        if author == "agent":
            await compress_history(sessionmaker, KEYS, uid, conv, models, policy=policy)
            continue
        rows = await _all_turns_safe(engine, conv)
        readable = [t for i, t in rows if i not in broken]
        env = await _summary(engine, conv)
        covered = _chain(summ, env["text"]) if env else []
        assert covered == readable[: len(covered)]
        if env:
            mark = next(k for k, (i, _) in enumerate(rows) if str(i) == env["through_id"])
            in_summary = [t for i, t in rows[: mark + 1] if i not in broken]
            assert in_summary == covered
            after = [t for i, t in rows[mark + 1 :] if i not in broken]
        else:
            after = readable
        ctx = await build_context(sessionmaker, KEYS, uid, conv, query=body)
        shown = [t.text for t in ctx.dialog]
        tail = [clip(t, 4_000) for t in after]
        assert shown == tail[len(tail) - len(shown) :]
        assert len(tail) - len(shown) == ctx.stats.dropped.get("dialog", 0)
    # После сбоев хвост догнан: после отметки — не больше порога.
    statuses: list[Any] = []
    for _ in range(120):
        r = await compress_history(sessionmaker, KEYS, uid, conv, models, policy=policy)
        statuses.append((r.status, r.error, r.rounds, r.more))
        if r.status == "skipped":
            break
    rows = await _all_turns_safe(engine, conv)
    env = await _summary(engine, conv)
    assert env is not None
    mark = next(k for k, (i, _) in enumerate(rows) if str(i) == env["through_id"])
    assert len(rows) - mark - 1 <= policy.trigger_turns, statuses[-3:]


async def _all_turns_safe(engine: AsyncEngine, conv: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    """Все реплики по порядку; нечитаемая — пустой строкой (отбрасывается по множеству id)."""
    async with engine.connect() as c:
        rows = await c.execute(
            text(
                "SELECT id, text_enc FROM conversation_messages WHERE conversation_id = :c "
                "ORDER BY created_at, id"
            ),
            {"c": conv},
        )
        out = []
        for r in rows:
            try:
                body = KEYS.open(crypto.CONVERSATION_MESSAGE_TEXT, r.id, bytes(r.text_enc))
            except crypto.DecryptionError:
                body = ""
            out.append((r.id, body))
        return out

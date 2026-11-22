"""2.1 этап 2, A8, раунд 2: независимая проверка исправлений A2 (коммит 3716528; АР-34).

Помощники — только свои (``tests/test_chat_api_a8.py``, ``tests/test_chat_a8.py``); модель — заглушки
без сети; БД — тестовая. Дефекты — ``xfail(strict=True)`` (docs/reviews/2.1-test.md, «Этап 2, раунд 2»)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app import help as help_mod
from app.ai.llm import (
    LLMBadOutput,
    LLMConfigError,
    LLMQuotaError,
    LLMRefusal,
    LLMUnavailable,
)
from app.api import chat as chat_api
from app.api.chat import ChatRuntime
from app.bot.poll import check_poll_config
from app.config import ConfigError, Settings
from app.core.limits import LimitDecision, MemoryDailyLimit
from app.help import check_help_contacts_config, parse_help_contacts
from app.repositories import chat as chat_repo
from tests.test_chat_a8 import FakeModel, _answer, _chunks, models_of
from tests.test_chat_api_a8 import (
    BOT_TOKEN,
    CRISIS,
    REPLY,
    SESSION_SECRET,
    World,
    assert_crisis,
    enter,
    post,
    stream_ok,
    su,
    turns_of,
    world_for,
)

NONE = {"category": "none", "secondary": []}


@pytest.fixture
async def w(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[World]:
    async with world_for(sessionmaker) as world:
        yield world


class ScreenBug(FakeModel):
    """Классификатор с ошибкой кода (не ``LLMError``)."""

    async def complete_json(self, **kw: Any) -> Any:
        if "category" in kw["schema"]["properties"]:
            raise RuntimeError("bug")
        return await super().complete_json(**kw)


NO_VERDICT: dict[str, Any] = {
    "refusal": LLMRefusal("no"),
    "unavailable": LLMUnavailable("503"),
    "quota": LLMQuotaError("spend"),
    "bad_output": LLMBadOutput("garbage"),
    "config": LLMConfigError("key"),
    "code_bug": "bug",
}


def _clf(reason: str) -> FakeModel:
    if reason == "code_bug":
        return ScreenBug()
    return FakeModel(verdict=NO_VERDICT[reason])


# =============================================================== 1. A8-E1: без вердикта — не 429


@pytest.mark.parametrize("reason", list(NO_VERDICT))
async def test_no_verdict_over_limit_goes_to_agent_and_counts(
    reason: str,
    w: World,
    engine: AsyncEngine,
) -> None:
    """АР-34 п.1, ТЗ §9.3, §2 п.7 (A8-E1): вторая линия вердикта не дала — по любой причине — при
    исчерпанном лимите 429 нет: реплика идёт к агенту (первая линия) и засчитывается."""
    w.env.limit = 1
    anna = await enter(w, "Анна")
    assert stream_ok(await post(w, anna, "раз"))[-1][0] == "done"
    agent = FakeModel(stream=_chunks(_answer(REPLY)))
    w.rt.models = models_of(agent, _clf(reason), w.summ)
    evs = stream_ok(await post(w, anna, "я больше не могу"))
    assert evs[-1][0] == "done"
    assert len(agent.kinds("stream")) == 1
    # Засчитана: при лимите 3 проходит ровно одна обычная реплика (1 + 1 + 1).
    w.rt.models = models_of(w.agent, w.clf, w.summ)
    w.env.limit = 3
    assert (await post(w, anna, "три")).status == 200
    assert (await post(w, anna, "четыре")).status == 429


async def test_no_models_over_limit_is_not_429(
    w: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """АР-34: модели не создались (вердикта нет) при исчерпанном лимите — не 429, а `error` с
    указанием на помощь."""

    async def boom(*a: Any, **k: Any) -> Any:
        raise LLMConfigError("нет ключа")

    w.env.limit = 1
    anna = await enter(w, "Анна")
    assert stream_ok(await post(w, anna, "раз"))[-1][0] == "done"
    monkeypatch.setattr(chat_api, "openrouter_models_async", boom)
    w.rt.models = None
    evs = stream_ok(await post(w, anna, "два"))
    assert evs[-1][0] == "error" and "112" in evs[-1][1]["message"]


# =============================================================== 2. Обе линии недоступны


AGENT_FAILS: dict[str, tuple[list[Any], str]] = {
    "refusal": ([LLMRefusal("no")], "refusal"),
    "unavailable": ([LLMUnavailable("503")], "unavailable"),
    "quota": ([LLMQuotaError("spend")], "quota"),
    "bad_output": (["не json"], "bad_output"),
    "after_chunks": ([*_chunks(_answer(REPLY))[:8], LLMUnavailable("x")], "unavailable"),
    "internal": ([LLMConfigError("key")], "internal"),
    "code_bug": ([RuntimeError("bug")], "internal"),
}


@pytest.mark.parametrize("verdict", ["no_verdict", "none"])
@pytest.mark.parametrize("fail", list(AGENT_FAILS))
async def test_both_lines_down_error_has_help_hint(
    fail: str,
    verdict: str,
    w: World,
) -> None:
    """АР-34 п.2, ТЗ §9.3 «всегда»: обе линии не ответили — `error` любого кода несёт фразу о
    помощи с номером 112 из файла контактов; при вердикте «не кризис» — без подсказки (обычное
    сообщение об ошибке)."""
    stream, code = AGENT_FAILS[fail]
    clf = FakeModel(verdict=LLMUnavailable("down") if verdict == "no_verdict" else NONE)
    w.rt.models = models_of(FakeModel(stream=stream), clf, w.summ)
    anna = await enter(w, "Анна")
    evs = stream_ok(await post(w, anna, "привет"))
    name, data = evs[-1]
    assert name == "error" and data["code"] == code
    if verdict == "no_verdict":
        assert "112" in data["message"] and "«Помощь»" in data["message"]
    else:
        assert chat_api.help_hint() not in data["message"] and "112" not in data["message"]


async def test_help_hint_phones_come_from_contacts_file(
    w: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """АР-33/34: номера в подсказке — из того же списка, что `GET /help/contacts` (не зашиты)."""
    other = parse_help_contacts(
        '{"verified_by_operator": false, "contacts": ['
        '{"name": "A", "phone": "8-800-A8", "description": "d"},'
        '{"name": "B", "phone": "112", "description": "d"}]}'
    )
    monkeypatch.setattr(help_mod, "HELP_CONTACTS", other)
    w.rt.models = models_of(FakeModel(stream=[LLMUnavailable("x")]),
                            FakeModel(verdict=LLMUnavailable("y")), w.summ)  # fmt: skip
    anna = await enter(w, "Анна")
    msg = stream_ok(await post(w, anna, "привет"))[-1][1]["message"]
    assert "8-800-A8" in msg and "112" in msg


async def test_context_failure_with_none_verdict_no_hint(
    w: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сборка контекста упала, классификатор сказал «не кризис» — `error internal` без подсказки."""

    async def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("ctx")

    monkeypatch.setattr(chat_api, "build_context", boom)
    anna = await enter(w, "Анна")
    data = stream_ok(await post(w, anna, "привет"))[-1][1]
    assert data["code"] == "internal" and chat_api.help_hint() not in data["message"]


# =============================================================== 3. Выключатель Redis


class HangingLimit:
    def __init__(self, delay: float = 5.0) -> None:
        self.calls = 0
        self.delay = delay
        self.broken = True
        self.inner = MemoryDailyLimit()

    async def acquire(self, user_id: uuid.UUID, limit: int, *, force: bool, **kw: Any) -> Any:
        self.calls += 1
        if self.broken:
            await asyncio.sleep(self.delay)
        return await self.inner.acquire(user_id, limit, force=force)


def _errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and "daily limit store unavailable" in r.getMessage()
    ]


async def test_breaker_timeout_pause_fallback_and_return(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """АР-34: зависший Redis — тайм-аут (`limit_timeout_s`), затем пауза: Redis не опрашивается,
    считает запасной счётчик (лимит не снят); одна запись `error` на паузу; после паузы —
    снова Redis."""
    caplog.set_level(logging.DEBUG)
    store = HangingLimit()
    rt = ChatRuntime(limiter=store, limit_timeout_s=0.1, limit_cooldown_s=0.5)
    uid = uuid.uuid4()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    first = await rt.acquire(uid, 3, force=False)
    assert first.allowed and loop.time() - t0 < 0.5
    t1 = loop.time()
    rest = [await rt.acquire(uid, 3, force=False) for _ in range(4)]
    assert loop.time() - t1 < 0.05  # во время паузы Redis не ждём
    assert store.calls == 1
    assert [d.allowed for d in rest] == [True, True, False, False]  # запасной счётчик держит лимит
    assert (await rt.acquire(uid, 3, force=True)).allowed  # кризис — всегда
    assert len(_errors(caplog)) == 1
    await asyncio.sleep(0.55)
    store.broken = False
    back = await rt.acquire(uid, 3, force=False)
    assert store.calls == 2 and back.allowed and back.count == 1  # снова Redis (свой счётчик)
    assert len(_errors(caplog)) == 1


async def test_breaker_store_exception(caplog: pytest.LogCaptureFixture) -> None:
    """Исключение хранилища — то же, что тайм-аут: пауза, запасной счётчик, одна запись `error`
    без id пользователя."""
    caplog.set_level(logging.DEBUG)

    class Boom:
        calls = 0

        async def acquire(self, *a: Any, **k: Any) -> LimitDecision:
            Boom.calls += 1
            raise ConnectionError("down")

    rt = ChatRuntime(limiter=Boom(), limit_cooldown_s=60)
    uid = uuid.uuid4()
    for _ in range(5):
        await rt.acquire(uid, 2, force=False)
    assert Boom.calls == 1 and len(_errors(caplog)) == 1
    assert str(uid) not in caplog.text


async def test_breaker_concurrent_burst(caplog: pytest.LogCaptureFixture) -> None:
    """Всплеск одновременных запросов в момент, когда Redis завис: каждый ждёт не больше
    тайм-аута; записи `error` — по одной на запрос, начатый до срабатывания (О-1 раунда)."""
    caplog.set_level(logging.DEBUG)
    store = HangingLimit()
    rt = ChatRuntime(limiter=store, limit_timeout_s=0.2, limit_cooldown_s=30)
    uid = uuid.uuid4()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    res = await asyncio.gather(*(rt.acquire(uid, 5, force=False) for _ in range(10)))
    assert loop.time() - t0 < 0.6
    assert sum(d.allowed for d in res) == 5
    assert 1 <= len(_errors(caplog)) <= 10


async def test_breaker_via_api(w: World, caplog: pytest.LogCaptureFixture) -> None:
    """Через API: при зависшем Redis ответы идут, лимит держит запасной счётчик, 429 — с
    `Retry-After`."""
    caplog.set_level(logging.DEBUG)
    w.rt.limiter = HangingLimit()
    w.rt.limit_timeout_s = 0.1
    w.env.limit = 2
    anna = await enter(w, "Анна")
    assert stream_ok(await post(w, anna, "a"))[-1][0] == "done"
    assert stream_ok(await post(w, anna, "b"))[-1][0] == "done"
    r = await post(w, anna, "c")
    assert r.status == 429 and int(r.headers["retry-after"]) >= 1
    assert len(_errors(caplog)) == 1


# =============================================================== 4. Сбой записи реплики владельца


async def test_owner_turn_save_failure(
    w: World,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ревью MINOR-1, ТЗ §9.3: реплику владельца сохранить не удалось — кризис по самой реплике:
    поток из одного `crisis` с `message_id: null` и контактами, событие безопасности записано,
    засчитано даже при исчерпанном лимите; не кризис — 500 (тело без текста реплики); 40P01 — 409."""

    async def boom(*a: Any, **k: Any) -> Any:
        raise OSError("db down")

    anna = await enter(w, "Анна")
    w.env.limit = 1
    assert stream_ok(await post(w, anna, "раз"))[-1][0] == "done"
    monkeypatch.setattr(chat_repo, "add_owner_turn", boom)
    w.clf.verdict = CRISIS
    agent_calls = len(w.agent.kinds("stream"))
    evs = stream_ok(await post(w, anna, "СЕКРЕТ-кризис"))
    assert [n for n, _ in evs] == ["crisis"]
    data = assert_crisis(evs)
    assert data["message_id"] is None
    await w.rt.drain()
    rows = await su(engine, "SELECT type::text FROM safety_events WHERE user_id = :u", u=anna.uid)
    assert [r[0] for r in rows] == ["self_harm"]
    assert len(w.agent.kinds("stream")) == agent_calls
    w.clf.verdict = NONE
    r = await post(w, anna, "СЕКРЕТ-обычная")
    assert r.status == 500 and "СЕКРЕТ" not in r.body
    assert ("owner", "СЕКРЕТ-обычная") not in await turns_of(engine, w, anna.uid)

    class Deadlock(Exception):
        sqlstate = "40P01"

    async def deadlock(*a: Any, **k: Any) -> Any:
        raise DBAPIError("INSERT", {}, Deadlock())

    monkeypatch.setattr(chat_repo, "add_owner_turn", deadlock)
    r = await post(w, anna, "x")
    assert r.status == 409 and r.json()["code"] == "conflict_retry"


@pytest.mark.xfail(
    strict=True,
    reason="A8-E2 MINOR: реплика не сохранилась и классификатор без вердикта — 500 без подсказки "
    "помощи (АР-34 п.2)",
)
async def test_owner_turn_save_failure_without_verdict_has_help(
    w: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """АР-34 п.2 («без вердикта второй линии — указание на помощь»), ТЗ §9.3: реплику владельца
    сохранить не удалось И классификатор вердикта не дал (БД и модель недоступны одновременно,
    отказ классификатора). Сейчас — `_unsaved_turn` бросает исходную ошибку → 500 с общим
    `internal` без номера помощи. Ожидается: ответ с указанием на помощь (поток `error` с
    `help_hint()` или 500 с ним в `message`)."""

    async def boom(*a: Any, **k: Any) -> Any:
        raise OSError("db down")

    anna = await enter(w, "Анна")
    monkeypatch.setattr(chat_repo, "add_owner_turn", boom)
    w.rt.models = models_of(w.agent, FakeModel(verdict=LLMRefusal("no")), w.summ)
    r = await post(w, anna, "хочу всё закончить")
    assert "112" in r.body


# =============================================================== 5. safety_events и уход клиента


async def test_safety_events_survive_client_leaving(
    w: World,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ревью MINOR-3, ТЗ §9.3 «событие фиксируется»: запись `safety_events` медленная, клиент
    ушёл, пока кризисный ответ ждёт её, — запись всё равно сделана (фоновая задача, уход
    клиента её не отменяет); кризис не ждёт записи дольше ~1 с."""
    real = chat_repo.record_safety_events

    async def slow(*a: Any, **k: Any) -> Any:
        await asyncio.sleep(0.6)
        return await real(*a, **k)

    monkeypatch.setattr(chat_repo, "record_safety_events", slow)
    w.clf.verdict = CRISIS
    anna = await enter(w, "Анна")
    gone = asyncio.Event()
    asyncio.get_running_loop().call_later(0.3, gone.set)
    r = await post(w, anna, "плохо", disconnect=gone)
    assert "event: crisis" not in r.body
    await w.rt.drain()
    rows = await su(engine, "SELECT type::text FROM safety_events WHERE user_id = :u", u=anna.uid)
    assert [x[0] for x in rows] == ["self_harm"]


async def test_crisis_not_delayed_by_slow_safety_events(
    w: World,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Контакты не задерживаются медленной записью события дольше `SAFETY_EVENTS_WAIT_S`;
    запись завершается в фоне."""
    real = chat_repo.record_safety_events

    async def slow(*a: Any, **k: Any) -> Any:
        await asyncio.sleep(3)
        return await real(*a, **k)

    monkeypatch.setattr(chat_repo, "record_safety_events", slow)
    w.clf.verdict = CRISIS
    anna = await enter(w, "Анна")
    r = await post(w, anna, "плохо")
    assert_crisis(stream_ok(r))
    t_crisis = next(t for t, c in r.chunks if "event: crisis" in c)
    assert t_crisis < chat_api.SAFETY_EVENTS_WAIT_S + 0.8
    await w.rt.drain()
    rows = await su(engine, "SELECT type::text FROM safety_events WHERE user_id = :u", u=anna.uid)
    assert [x[0] for x in rows] == ["self_harm"]


@pytest.mark.xfail(
    strict=True,
    reason="A8-E3 MINOR: остановка сервиса (ChatRuntime.aclose) отменяет незаписанные "
    "safety_events вместе со сжатием",
)
async def test_shutdown_waits_for_safety_events(
    w: World,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ТЗ §9.3 «событие фиксируется»: остановка сервиса сразу после кризисного ответа не должна
    терять запись `safety_events` (её можно дождаться — до `SAFETY_EVENTS_TIMEOUT_S`); сжатие —
    можно снять. Сейчас `aclose` отменяет все фоновые задачи без разбора."""
    real = chat_repo.record_safety_events

    async def slow(*a: Any, **k: Any) -> Any:
        await asyncio.sleep(1.5)
        return await real(*a, **k)

    monkeypatch.setattr(chat_repo, "record_safety_events", slow)
    w.clf.verdict = CRISIS
    anna = await enter(w, "Анна")
    assert_crisis(stream_ok(await post(w, anna, "плохо")))
    await w.rt.aclose()
    rows = await su(engine, "SELECT type::text FROM safety_events WHERE user_id = :u", u=anna.uid)
    assert [x[0] for x in rows] == ["self_harm"]


# =============================================================== 6. Окружение по умолчанию


def _bare_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.delenv("FEELINGS_ENV", raising=False)
    return Settings(_env_file=None)


def test_default_env_is_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ревью MINOR-4, АР-33: без `FEELINGS_ENV` (и без `.env`) — продуктив: непроверенный список
    контактов не пускает сервис, опрос бота запрещён (до любого вызова Bot API)."""
    s = _bare_settings(monkeypatch)
    assert s.env == "production"
    with pytest.raises(ConfigError):
        check_help_contacts_config(s)
    s2 = s.model_copy(
        update={
            "telegram_bot_token": SecretStr(BOT_TOKEN),
            "webapp_url": "https://a8.example.test/app",
            "session_secret": SecretStr(SESSION_SECRET),
        }
    )
    with pytest.raises(ConfigError, match="development only"):
        check_poll_config(s2)
    for dev in ("dev", "test", "local"):
        check_poll_config(s2.model_copy(update={"env": dev}))
        check_help_contacts_config(s2.model_copy(update={"env": dev}))


def test_env_example_sets_dev_explicitly() -> None:
    """`.env.example` задаёт `FEELINGS_ENV=dev` явно (разработчик не получает продуктив случайно)."""
    from pathlib import Path

    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
    assert "\nFEELINGS_ENV=dev\n" in example


# =============================================================== 7. Одна задача сжатия на диалог


async def test_one_compression_per_dialog_rerun_and_slots() -> None:
    """Ревью MINOR-5: на диалог — одна задача сжатия; запрос во время работы — повтор после неё
    (не параллельно); одновременно не больше `COMPRESSION_SLOTS` диалогов; после — задач нет."""
    rt = ChatRuntime()
    running: dict[uuid.UUID, int] = {}
    peak_total = 0
    runs: dict[uuid.UUID, int] = {}
    gate = asyncio.Event()

    def job(conv: uuid.UUID) -> Any:
        async def go() -> None:
            nonlocal peak_total
            running[conv] = running.get(conv, 0) + 1
            assert running[conv] == 1, "два сжатия одного диалога одновременно"
            peak_total = max(peak_total, sum(running.values()))
            runs[conv] = runs.get(conv, 0) + 1
            await gate.wait()
            await asyncio.sleep(0.01)
            running[conv] -= 1

        return go

    convs = [uuid.uuid4() for _ in range(5)]
    for c in convs:
        rt.compress(c, job(c))
    for _ in range(3):
        rt.compress(convs[0], job(convs[0]))  # пока идёт — только отметка повтора
    await asyncio.sleep(0.05)
    assert peak_total <= chat_api.COMPRESSION_SLOTS
    assert len(rt.background) == 5
    gate.set()
    await rt.drain()
    assert runs[convs[0]] == 2 and all(runs[c] == 1 for c in convs[1:])
    assert peak_total == chat_api.COMPRESSION_SLOTS
    assert rt.background == set() and rt._compressing == {} and rt._compress_again == set()
    rt.compress(convs[0], job(convs[0]))  # после завершения — снова можно
    await rt.drain()
    assert runs[convs[0]] == 3


async def test_compression_cancel_cleans_state() -> None:
    """Остановка (`aclose`) во время сжатия и ожидания слота — задачи сняты, состояние чистое."""
    rt = ChatRuntime()
    started = asyncio.Event()

    async def forever() -> None:
        started.set()
        await asyncio.sleep(30)

    for _ in range(4):
        rt.compress(uuid.uuid4(), forever)
    await started.wait()
    await rt.aclose()
    assert rt.background == set() and rt._compressing == {}


async def test_one_compression_per_dialog_via_api(
    w: World,
    engine: AsyncEngine,
) -> None:
    """Через API: пока сжатие диалога ждёт модель, новые ответы не запускают второе сжатие того
    же диалога; после — повтор, затем задач нет."""
    in_flight = 0
    peak = 0
    gate = asyncio.Event()

    class SlowSumm(FakeModel):
        async def complete_json(self, **kw: Any) -> Any:
            nonlocal in_flight, peak
            if "summary" in kw["schema"]["properties"]:
                in_flight += 1
                peak = max(peak, in_flight)
                try:
                    await gate.wait()
                finally:
                    in_flight -= 1
            return await super().complete_json(**kw)

    from app.ai.summary import CompressionPolicy

    w.rt.models = models_of(w.agent, w.clf, SlowSumm(summary={"summary": "S"}))
    w.rt.compression = CompressionPolicy(trigger_turns=7, keep_turns=6)
    anna = await enter(w, "Анна")
    for i in range(8):
        assert stream_ok(await post(w, anna, f"m{i}"))[-1][0] == "done"
    await asyncio.sleep(0.05)
    assert peak == 1 and len(w.rt.background) == 1
    gate.set()
    await w.rt.drain()
    assert w.rt.background == set() and peak == 1

"""3.5 р.2 (A8): негативные случаи миграции 0015 — ``pipeline_notify_release`` и новое условие
«свободно» в ``pipeline_claim_notify``.

ФТ-18; ТЗ §4.4 п.3, §9.1 п.5, §10; DECISIONS.md АР-13, АР-38, АР-44, АР-46; ревью 3.5 р.1 MINOR-1.
Дополняет ``test_notify_release_db.py`` (A1): чужой и устаревший (перехваченный) токен, токен
другого сообщения, граничные отсрочки, вызов чужими ролями (в откатываемых транзакциях), гонки
снятия, захвата и отметки двух копий, новый код воркера на схеме 0014 (откат миграции).

Данные выдуманные; общие помощники — из ``test_pipeline_db.py``."""

import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import make_sessionmaker
from app.repositories import notify_work
from tests.conftest import alembic
from tests.test_couple_functions import SM, _sqlstate
from tests.test_notify_release_db import NOTIFY_RELEASE, _claim_notify, _delivered, _lease
from tests.test_notify_worker import FakeTg, nworker, statuses, tg_error
from tests.test_pipeline_db import (
    CLAIM_NOTIFY,
    LEASE,
    NOTIFY_DONE,
    _claim,
    _expire,
    _msg,
    _pair,
    _park,
    _send,
    _w1,
    _worker_tx,
)


async def _setup(sm: SM, engine: AsyncEngine) -> tuple[Any, Any, uuid.UUID, Any]:
    await _park(engine)
    anna, igor, _ = await _pair(sm, engine)
    mid = await _delivered(sm, engine, anna)
    claim = (await _claim_notify(engine))[mid]
    return anna, igor, mid, claim


# --------------------------------------------------------------------------- токены


async def test_stale_token_after_takeover_does_not_release_new_lease(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Копия A «зависла», аренда истекла, копия B перехватила: снятие A по устаревшему токену
    — ``false``, аренда B и счётчик попыток не тронуты; B отмечает своим токеном."""
    _a, _i, mid, a = await _setup(sessionmaker, engine)
    await _expire(engine, mid)
    b = (await _claim_notify(engine))[mid]
    assert b.token != a.token and b.attempts == 2
    before = await _lease(engine, mid)
    for r in (None, "1 hour"):
        assert await _w1(engine, NOTIFY_RELEASE, m=mid, t=a.token, r=r, c=False) is False
    assert await _lease(engine, mid) == before
    assert await _w1(engine, NOTIFY_DONE, m=mid, t=b.token) is True


async def test_token_of_other_message_rejected(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Токен аренды другого сообщения (своей же копии) не снимает аренду этого: ``false``."""
    await _park(engine)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    m1 = await _delivered(sessionmaker, engine, anna, b"1")
    m2 = await _delivered(sessionmaker, engine, anna, b"2")
    got = await _claim_notify(engine)
    before = (await _lease(engine, m1), await _lease(engine, m2))
    assert await _w1(engine, NOTIFY_RELEASE, m=m1, t=got[m2].token, r=None, c=False) is False
    assert await _w1(engine, NOTIFY_RELEASE, m=m2, t=got[m1].token, r=None, c=False) is False
    assert (await _lease(engine, m1), await _lease(engine, m2)) == before


async def test_pipeline_token_of_other_message_on_notify_lease(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Токен фазы конвейера (другое сообщение в ``processing``) на сообщении в фазе
    уведомления — ``false``; аренда конвейера тоже не тронута."""
    anna, _igor, mid, _c = await _setup(sessionmaker, engine)
    proc, _ = await _send(sessionmaker, engine, anna, b"p")
    ptok = (await _claim(engine))[proc].token
    before = (await _lease(engine, mid), await _lease(engine, proc))
    assert await _w1(engine, NOTIFY_RELEASE, m=mid, t=ptok, r=None, c=False) is False
    assert (await _lease(engine, mid), await _lease(engine, proc)) == before


# --------------------------------------------------------------------------- отсрочка


@pytest.mark.parametrize("zero", ["0 seconds", "00:00:00", None])
async def test_zero_or_null_delay_is_immediately_claimable(
    sessionmaker: SM, engine: AsyncEngine, zero: str | None
) -> None:
    """Отсрочка 0 и NULL — срок не ставится (``work_lease_until`` NULL), следующий захват
    выдаёт сразу; попытка не расходуется."""
    _a, _i, mid, c = await _setup(sessionmaker, engine)
    assert await _w1(engine, NOTIFY_RELEASE, m=mid, t=c.token, r=zero, c=False) is True
    assert await _lease(engine, mid) == (None, None, 0)
    again = (await _claim_notify(engine))[mid]
    assert again.attempts == 1


@pytest.mark.parametrize(
    "bad", ["1 day 00:00:00.001", "25 hours", "1 mon", "1 year", "-0.001 seconds", "-1 day"]
)
async def test_delay_out_of_range_rejected_without_change(
    sessionmaker: SM, engine: AsyncEngine, bad: str
) -> None:
    """Отсрочка больше 1 дня (в т.ч. месяц/год) и отрицательная — 22023, аренда не тронута."""
    _a, _i, mid, c = await _setup(sessionmaker, engine)
    before = await _lease(engine, mid)
    with pytest.raises(DBAPIError) as ei:
        await _w1(engine, NOTIFY_RELEASE, m=mid, t=c.token, r=bad, c=False)
    assert _sqlstate(ei.value) == "22023", bad
    assert await _lease(engine, mid) == before


async def test_exactly_one_day_allowed_and_holds(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Граница: ровно 24 часа допустимо; захват до срока не выдаёт."""
    _a, _i, mid, c = await _setup(sessionmaker, engine)
    assert await _w1(engine, NOTIFY_RELEASE, m=mid, t=c.token, r="24 hours", c=False) is True
    token, until, attempts = await _lease(engine, mid)
    assert token is None and until is not None and attempts == 0
    assert await _claim_notify(engine) == {}


# --------------------------------------------------------------------------- роли


@pytest.mark.parametrize("role", ["feelings_app", "feelings_api", "feelings_admin"])
async def test_other_roles_cannot_execute(sessionmaker: SM, engine: AsyncEngine, role: str) -> None:
    """Вызов не от ``feelings_worker`` — 42501 (транзакция откатывается; роли общие для
    кластера не меняются); аренда не тронута."""
    _a, _i, mid, c = await _setup(sessionmaker, engine)
    async with engine.connect() as conn:
        tx = await conn.begin()
        try:
            await conn.execute(text(f"SET LOCAL ROLE {role}"))
            with pytest.raises(DBAPIError) as ei:
                await conn.execute(
                    text(NOTIFY_RELEASE), {"m": mid, "t": c.token, "r": None, "c": False}
                )
            assert _sqlstate(ei.value) == "42501", role
        finally:
            await tx.rollback()
    assert (await _lease(engine, mid))[0] == c.token


# --------------------------------------------------------------------------- гонки


async def _blocked(task: "asyncio.Task[Any]") -> None:
    await asyncio.sleep(0.5)
    assert not task.done(), "вызов не ждал блокировки строки"


async def test_claim_skips_row_while_release_uncommitted(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Копия A снимает аренду (транзакция не завершена) — захват копии B строку пропускает
    (``SKIP LOCKED``), не ждёт; после фиксации без отсрочки — B получает её, попытка та же."""
    _a, _i, mid, c = await _setup(sessionmaker, engine)
    async with _worker_tx(engine) as a:
        res = await a.execute(text(NOTIFY_RELEASE), {"m": mid, "t": c.token, "r": None, "c": False})
        assert res.scalar_one() is True
        got = await asyncio.wait_for(_claim_notify(engine), 5)
        assert mid not in got
    again = (await _claim_notify(engine))[mid]
    assert again.attempts == 1 and again.token != c.token


async def test_stale_release_waits_for_concurrent_takeover_then_false(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Аренда A истекла; захват B ещё не зафиксирован (строка заблокирована, новый токен), A
    в этот момент снимает аренду старым токеном — ждёт B и после фиксации получает ``false``:
    аренда B цела, отметка B проходит (ровно одно уведомление)."""
    _a, _i, mid, a = await _setup(sessionmaker, engine)
    await _expire(engine, mid)
    async with _worker_tx(engine) as b:
        rows = list(await b.execute(text(CLAIM_NOTIFY), {"l": LEASE, "n": 3, "lim": 100}))
        b_tok = {r.message_id: r for r in rows}[mid].token
        stale = asyncio.create_task(_w1(engine, NOTIFY_RELEASE, m=mid, t=a.token, r=None, c=False))
        await _blocked(stale)
    assert await asyncio.wait_for(stale, 5) is False
    token, _until, attempts = await _lease(engine, mid)
    assert token == b_tok and attempts == 2
    assert await _w1(engine, NOTIFY_DONE, m=mid, t=b_tok) is True


async def test_release_racing_done_same_token_one_wins(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Отметка и снятие по одному токену одновременно (повтор после потерянного ответа):
    отметка зафиксирована первой — снятие ``false``, отметка не снимается и уведомление не
    возвращается в очередь."""
    _a, _i, mid, c = await _setup(sessionmaker, engine)
    async with _worker_tx(engine) as d:
        res = await d.execute(text(NOTIFY_DONE), {"m": mid, "t": c.token})
        assert res.scalar_one() is True
        rel = asyncio.create_task(_w1(engine, NOTIFY_RELEASE, m=mid, t=c.token, r=None, c=False))
        await _blocked(rel)
    assert await asyncio.wait_for(rel, 5) is False
    row = await _msg(engine, mid)
    assert row is not None and row["notify_done_at"] is not None
    await _expire(engine, mid)
    assert mid not in await _claim_notify(engine)


async def test_double_release_same_token_concurrently_once(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Два снятия одним токеном одновременно: ровно одно ``true``; счётчик уменьшен один раз
    (не уходит ниже нуля и не «дарит» попытку)."""
    _a, _i, mid, c = await _setup(sessionmaker, engine)
    async with _worker_tx(engine) as a:
        res = await a.execute(text(NOTIFY_RELEASE), {"m": mid, "t": c.token, "r": None, "c": False})
        assert res.scalar_one() is True
        second = asyncio.create_task(_w1(engine, NOTIFY_RELEASE, m=mid, t=c.token, r=None, c=False))
        await _blocked(second)
    assert await asyncio.wait_for(second, 5) is False
    assert await _lease(engine, mid) == (None, None, 0)


# --------------------------------------------------------------------------- откат 0015


async def test_new_worker_on_0014_schema_degrades_to_lease(
    sessionmaker: SM, engine: AsyncEngine, app_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """Откат 0015 при уже выложенном коде воркера (порядок выкладки нарушен): 401 — пауза,
    снятие аренды падает (функции нет), процесс не падает, отметки нет, аренда держится до
    истечения (поведение до 0015). После upgrade — снятие снова работает; ``alembic check``."""
    _a, _i, mid, _c = await _setup(sessionmaker, engine)
    await _expire(engine, mid)
    alembic("downgrade", "0014")
    await engine.dispose()
    await app_engine.dispose()
    try:
        tg = FakeTg(tg_error(401))
        w = nworker(engine, tg)
        caplog.set_level(logging.INFO, logger="app.workers.notify")
        assert statuses(await w.run_once()) == [("paused", "http_401")]
        await w.api.aclose()
        assert "notify release failed: ProgrammingError" in "\n".join(
            r.getMessage() for r in caplog.records
        )
        row = await _msg(engine, mid)
        assert row is not None and row["notify_done_at"] is None
        assert row["work_lease_token"] is not None
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    alembic("check")
    await _expire(engine, mid)
    c = (await _claim_notify(engine))[mid]
    released = await notify_work.release(
        make_sessionmaker(engine),
        notify_work.NotifyClaim(c.message_id, c.recipient_id, c.token, c.attempts),
        retry_after=timedelta(minutes=5),
        count_attempt=False,
    )
    assert released is True
    assert await _claim_notify(engine) == {}

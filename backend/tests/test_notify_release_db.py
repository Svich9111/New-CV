"""3.5 р.1 (A1): снятие аренды уведомления без расхода попытки — миграция 0015.

ФТ-18; ТЗ §4.4 п.3, §10; DECISIONS.md АР-13, АР-38, АР-44, АР-45, АР-46; ревью 3.5 р.1 MINOR-1.

`pipeline_notify_release` — для 401/404 настройки бота и 429 дольше окна аренды: уведомление
не теряется через `max_attempts` пауз, а ждёт исправления. Данные выдуманные; общие помощники —
из `test_pipeline_db.py` (чужие строки очередей «паркуются» долгой арендой)."""

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
from tests.test_couple_functions import LEAVE, SM, _call, _sqlstate
from tests.test_pipeline_db import (
    CLAIM_NOTIFY,
    DELETE,
    LEASE,
    NOTIFY_DONE,
    RELEASE,
    P,
    _claim,
    _deliver,
    _expire,
    _msg,
    _pair,
    _park,
    _send,
    _w,
    _w1,
    _worker_tx,
)

FUNC = "pipeline_notify_release(uuid, uuid, interval, boolean)"
NOTIFY_RELEASE = (
    "SELECT pipeline_notify_release(:m, :t, CAST(CAST(:r AS text) AS interval), "
    "CAST(:c AS boolean))"
)


async def _claim_notify(engine: AsyncEngine, n: int = 3) -> dict[uuid.UUID, Any]:
    return {r.message_id: r for r in await _w(engine, CLAIM_NOTIFY, l=LEASE, n=n, lim=100)}


async def _delivered(sm: SM, engine: AsyncEngine, author: P, body: bytes = b"m") -> uuid.UUID:
    """Сообщение автора прошло конвейер — в очереди уведомлений."""
    mid, _ = await _send(sm, engine, author, body)
    assert await _deliver(engine, mid, (await _claim(engine))[mid].token) == "delivered"
    return mid


async def _lease(engine: AsyncEngine, m: uuid.UUID) -> tuple[Any, Any, Any]:
    row = await _msg(engine, m)
    assert row is not None
    return row["work_lease_token"], row["work_lease_until"], row["work_attempts"]


# --------------------------------------------------------------------------- права


async def test_privileges_only_worker(engine: AsyncEngine) -> None:
    """EXECUTE — только `feelings_worker`; SECURITY DEFINER, `search_path = public, pg_temp`."""
    async with engine.connect() as conn:

        async def q(sql: str, **p: Any) -> Any:
            return (await conn.execute(text(sql), p)).scalar_one()

        for role in ("feelings_app", "feelings_api", "feelings_admin", "public"):
            assert not await q("SELECT has_function_privilege(:r, :f, 'EXECUTE')", r=role, f=FUNC)
        assert await q("SELECT has_function_privilege('feelings_worker', :f, 'EXECUTE')", f=FUNC)
        assert await q("SELECT prosecdef FROM pg_proc WHERE oid = to_regprocedure(:f)", f=FUNC)
        cfg = await q("SELECT proconfig FROM pg_proc WHERE oid = to_regprocedure(:f)", f=FUNC)
        assert "search_path=public, pg_temp" in cfg


async def test_app_role_cannot_call(sessionmaker: SM, engine: AsyncEngine) -> None:
    await _park(engine)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    mid = await _delivered(sessionmaker, engine, anna)
    tok = (await _claim_notify(engine))[mid].token
    with pytest.raises(DBAPIError) as ei:
        await _call(sessionmaker, anna.user_id, NOTIFY_RELEASE, m=mid, t=tok, r=None, c=False)
    assert _sqlstate(ei.value) == "42501"
    assert (await _lease(engine, mid))[0] == tok  # аренда не тронута


# --------------------------------------------------------------------------- поведение


async def test_own_token_releases_without_counting_attempt(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Свой токен: аренда снята, попытка не расходуется; чужой/старый токен — без эффекта."""
    await _park(engine)
    anna, igor, _ = await _pair(sessionmaker, engine)
    mid = await _delivered(sessionmaker, engine, anna)
    first = (await _claim_notify(engine))[mid]
    assert first.attempts == 1 and first.recipient_id == igor.user_id
    before = await _lease(engine, mid)
    assert await _w1(engine, NOTIFY_RELEASE, m=mid, t=uuid.uuid4(), r=None, c=False) is False
    assert await _w1(engine, NOTIFY_RELEASE, m=mid, t=None, r=None, c=False) is False
    assert await _lease(engine, mid) == before  # чужой токен — ничего не изменено
    assert await _w1(engine, NOTIFY_RELEASE, m=mid, t=first.token, r=None, c=False) is True
    assert await _lease(engine, mid) == (None, None, 0)
    assert await _w1(engine, NOTIFY_RELEASE, m=mid, t=first.token, r=None, c=False) is False
    assert await _w1(engine, NOTIFY_DONE, m=mid, t=first.token) is False  # старый токен
    second = (await _claim_notify(engine))[mid]  # сразу снова в очереди, попытка — снова первая
    assert second.attempts == 1 and second.token != first.token
    assert await _w1(engine, NOTIFY_DONE, m=mid, t=second.token) is True
    assert await _w1(engine, NOTIFY_RELEASE, m=mid, t=second.token, r=None, c=False) is False
    row = await _msg(engine, mid)
    assert row is not None and row["notify_done_at"] is not None


async def test_counted_attempt_and_retry_after(sessionmaker: SM, engine: AsyncEngine) -> None:
    """`p_count_attempt = true` — попытка остаётся израсходованной; `p_retry_after` — захват
    уведомления не выдаёт сообщение раньше срока, после срока — выдаёт (следующая попытка)."""
    await _park(engine)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    mid = await _delivered(sessionmaker, engine, anna)
    first = (await _claim_notify(engine))[mid]
    assert await _w1(engine, NOTIFY_RELEASE, m=mid, t=first.token, r="1 hour", c=True) is True
    token, until, attempts = await _lease(engine, mid)
    assert token is None and until is not None and attempts == 1
    assert await _claim_notify(engine) == {}  # «не раньше» срока
    await _expire(engine, mid)
    second = (await _claim_notify(engine))[mid]
    assert second.attempts == 2
    # Попытки исчерпаны (n = 2) — при следующем захвате отметка без выдачи.
    assert await _w1(engine, NOTIFY_RELEASE, m=mid, t=second.token, r=None, c=True) is True
    assert await _claim_notify(engine, n=2) == {}
    row = await _msg(engine, mid)
    assert row is not None and row["notify_done_at"] is not None


async def test_attempts_not_exhausted_by_paused_releases(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """MINOR-1: пауза при ошибке настройки бота сколько угодно раз — уведомление не помечается
    сделанным без отправки (предел попыток не достигается)."""
    await _park(engine)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    mid = await _delivered(sessionmaker, engine, anna)
    for _ in range(5):
        c = (await _claim_notify(engine, n=1))[mid]
        assert c.attempts == 1
        assert await _w1(engine, NOTIFY_RELEASE, m=mid, t=c.token, r="5 minutes", c=False) is True
        assert await _claim_notify(engine, n=1) == {}
        await _expire(engine, mid)
    row = await _msg(engine, mid)
    assert row is not None and row["notify_done_at"] is None and row["status"] == "delivered"


async def test_phases_do_not_cross(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Токен конвейера не снимается `pipeline_notify_release`, токен уведомления —
    `pipeline_release` (фазы аренды не пересекаются)."""
    await _park(engine)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    proc, _ = await _send(sessionmaker, engine, anna, b"p")
    ptok = (await _claim(engine))[proc].token
    assert await _w1(engine, NOTIFY_RELEASE, m=proc, t=ptok, r=None, c=False) is False
    assert (await _lease(engine, proc))[0] == ptok
    assert await _deliver(engine, proc, ptok) == "delivered"
    ntok = (await _claim_notify(engine))[proc].token
    assert await _w1(engine, RELEASE, m=proc, t=ntok, r=None, c=False) is False
    assert (await _lease(engine, proc))[0] == ntok


async def test_read_during_lease_and_gone_message(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Получатель прочитал, пока шла отправка — аренда снимается (как отметка, 0013);
    сообщения нет (аккаунт автора удалён) — `false`."""
    await _park(engine)
    anna, igor, _ = await _pair(sessionmaker, engine)
    read = await _delivered(sessionmaker, engine, anna, b"r")
    gone = await _delivered(sessionmaker, engine, anna, b"g")
    got = await _claim_notify(engine)
    assert await _call(sessionmaker, igor.user_id, "SELECT app_mark_read(:m)", m=read) is True
    assert await _w1(engine, NOTIFY_RELEASE, m=read, t=got[read].token, r=None, c=False) is True
    assert await _lease(engine, read) == (None, None, 0)
    await _call(sessionmaker, anna.user_id, DELETE)
    assert await _msg(engine, gone) is None
    assert await _w1(engine, NOTIFY_RELEASE, m=gone, t=got[gone].token, r=None, c=False) is False


async def test_argument_checks(sessionmaker: SM, engine: AsyncEngine) -> None:
    await _park(engine)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    mid = await _delivered(sessionmaker, engine, anna)
    tok = (await _claim_notify(engine))[mid].token
    for r, c in (("-1 second", True), ("1 day 1 second", False), (None, None)):
        with pytest.raises(DBAPIError) as ei:
            await _w(engine, NOTIFY_RELEASE, m=mid, t=tok, r=r, c=c)
        assert _sqlstate(ei.value) == "22023", (r, c)
    assert (await _lease(engine, mid))[0] == tok
    assert await _w1(engine, NOTIFY_RELEASE, m=mid, t=tok, r="1 day", c=False) is True


async def test_release_does_not_wait_for_couple_or_user_locks(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """АР-13/АР-38: меняется одна строка сообщения — удаление аккаунта/выход, держащие пару и
    обоих пользователей FOR UPDATE, снятие не задерживают."""
    await _park(engine)
    anna, igor, couple = await _pair(sessionmaker, engine)
    mid = await _delivered(sessionmaker, engine, anna)
    tok = (await _claim_notify(engine))[mid].token
    async with engine.connect() as holder, holder.begin():
        await holder.execute(text("SELECT 1 FROM couples WHERE id = :c FOR UPDATE"), {"c": couple})
        await holder.execute(
            text("SELECT 1 FROM users WHERE id = ANY(:u) FOR UPDATE"),
            {"u": [anna.user_id, igor.user_id]},
        )
        async with _worker_tx(engine) as w:
            await w.execute(text("SET LOCAL lock_timeout = '1s'"))
            res = await w.execute(
                text(NOTIFY_RELEASE), {"m": mid, "t": tok, "r": "5 seconds", "c": False}
            )
            assert res.scalar_one() is True


async def test_dissolved_couple_after_release_marked_not_sent(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Пара распалась, пока уведомление ждало (пауза) — следующий захват ставит отметку без
    выдачи (как 0013), отсрочка этому не мешает."""
    await _park(engine)
    anna, igor, _ = await _pair(sessionmaker, engine)
    mid = await _delivered(sessionmaker, engine, anna)
    tok = (await _claim_notify(engine))[mid].token
    assert await _w1(engine, NOTIFY_RELEASE, m=mid, t=tok, r="1 hour", c=False) is True
    await _call(sessionmaker, igor.user_id, LEAVE)
    await _expire(engine, mid)
    assert await _claim_notify(engine) == {}
    row = await _msg(engine, mid)
    assert row is not None and row["notify_done_at"] is not None


async def test_repository_release(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Обёртка `notify_work.release` (для воркера 3.5): отсрочка обрезается до 1 дня, `False` —
    не своя аренда."""
    await _park(engine)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    mid = await _delivered(sessionmaker, engine, anna)
    wsm = make_sessionmaker(engine)
    claims = {
        c.message_id: c
        for c in await notify_work.claim(wsm, lease=timedelta(minutes=2), max_attempts=3, limit=10)
    }
    c = claims[mid]
    stale = notify_work.NotifyClaim(c.message_id, c.recipient_id, uuid.uuid4(), c.attempts)
    assert await notify_work.release(wsm, stale, retry_after=timedelta(0), count_attempt=False) is (
        False
    )
    assert (
        await notify_work.release(wsm, c, retry_after=timedelta(days=5), count_attempt=False)
        is True
    )
    token, until, attempts = await _lease(engine, mid)
    assert token is None and attempts == 0
    async with engine.connect() as conn:
        left = (
            await conn.execute(text("SELECT CAST(:u AS timestamptz) - now()"), {"u": until})
        ).scalar_one()
    assert timedelta(hours=23) < left <= timedelta(days=1)
    assert await notify_work.claim(wsm, lease=timedelta(minutes=2), max_attempts=3, limit=10) == []


# --------------------------------------------------------------------------- обратимость


async def test_downgrade_0014_upgrade_check(
    sessionmaker: SM, engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """0015 → 0014 → head: функция удаляется, `pipeline_claim_notify` — тело 0013 (отсрочка без
    токена не держит), upgrade возвращает отсрочку; `alembic check` чист."""
    await _park(engine)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    mid = await _delivered(sessionmaker, engine, anna)
    tok = (await _claim_notify(engine))[mid].token
    assert await _w1(engine, NOTIFY_RELEASE, m=mid, t=tok, r="1 hour", c=False) is True
    alembic("downgrade", "0014")
    await engine.dispose()
    await app_engine.dispose()
    try:
        async with engine.connect() as conn:
            n = await conn.execute(
                text("SELECT count(*) FROM pg_proc WHERE proname = 'pipeline_notify_release'")
            )
            assert n.scalar_one() == 0
            body = await conn.execute(
                text(
                    "SELECT pg_get_functiondef('pipeline_claim_notify(interval, integer, "
                    "integer)'::regprocedure)"
                )
            )
            assert "m.work_lease_token IS NULL OR m.work_lease_until < now()" in body.scalar_one()
            ok = await conn.execute(
                text(
                    "SELECT has_function_privilege('feelings_worker', "
                    "'pipeline_claim_notify(interval, integer, integer)', 'EXECUTE')"
                )
            )
            assert ok.scalar_one() is True
        assert set(await _claim_notify(engine)) == {mid}  # 0013: строка без токена свободна
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    alembic("check")
    async with engine.connect() as conn:
        ok = await conn.execute(
            text("SELECT has_function_privilege('feelings_worker', :f, 'EXECUTE')"), {"f": FUNC}
        )
        assert ok.scalar_one() is True
    assert await _claim_notify(engine) == {}  # аренда, взятая на 0014, ещё жива
    await _expire(engine, mid)
    again = (await _claim_notify(engine))[mid]
    assert await _w1(engine, NOTIFY_RELEASE, m=mid, t=again.token, r="1 hour", c=False) is True
    assert await _claim_notify(engine) == {}  # после upgrade отсрочка снова держит

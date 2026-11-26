"""3.2 (A1): воркер конвейера входящего на уровне БД — миграция 0013.

ФТ-09; ТЗ §2 п.2–3, п.7, §4.5 п.2, §6.2 п.3–7, §9.3, §10; DECISIONS.md АР-11, АР-13, АР-27, АР-28,
АР-38, АР-44; BACKLOG Б-39, Б-44, Б-45, Б-47, Б-48.

Данные — выдуманные: заводит суперпользователь (`engine`), отправка — `app_send_draft` под ролью
входа приложения (`user_scope`), конвейер — под `feelings_worker` (`SET LOCAL ROLE`). Перед
каждым захватом чужие строки очередей «паркуются» долгой арендой: тесты общей БД не забирают
сообщения друг друга."""

import asyncio
import itertools
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db import user_scope
from tests.conftest import alembic
from tests.test_account_delete import _hold_and_race
from tests.test_couple_functions import (
    ACCEPT,
    LEAVE,
    ONBOARDED,
    SM,
    _call,
    _sqlstate,
    _token,
    _try,
    _wait_lock_waiters,
)

_tg = itertools.count(9_610_000)
LEASE = "5 minutes"

CLAIM = "SELECT * FROM pipeline_claim(CAST(CAST(:l AS text) AS interval), :n, :lim)"
READ = "SELECT * FROM pipeline_read(:m, :t)"
DELIVER = (
    "SELECT pipeline_deliver_leased(:m, :t, :b, :ad, CAST(:tone AS smallint), :h, "
    "CAST(CAST(:ev AS text[]) AS safety_event_type[]))"
)
# 0014: 5 аргументов — тип кризиса (`safety` требует его). 4-аргументная обёртка 0013 снята в
# 0016 (Б-83, АР-48): `cr` в вызовах ниже — тот тип, который выводила она (первый не-`underage`).
WITHHOLD = (
    "SELECT pipeline_withhold_leased(:m, :t, :r, CAST(CAST(:ev AS text[]) AS safety_event_type[]), "
    "CAST(CAST(:cr AS text) AS safety_event_type))"
)
CLAIM_NOTIFY = "SELECT * FROM pipeline_claim_notify(CAST(CAST(:l AS text) AS interval), :n, :lim)"
NOTIFY_DONE = "SELECT pipeline_notify_done(:m, :t)"
DELETE = "SELECT app_delete_account()"
SEND = "SELECT app_send_draft(:d, 1)"

NEW_WORKER_FUNCS = (
    "pipeline_claim(interval, integer, integer)",
    "pipeline_read(uuid, uuid)",
    "pipeline_deliver_leased(uuid, uuid, bytea, boolean, smallint, boolean, safety_event_type[])",
    "pipeline_withhold_leased(uuid, uuid, character varying, safety_event_type[], "
    "safety_event_type)",  # 0014 (4-аргументная 0013 снята в 0016)
    "pipeline_claim_notify(interval, integer, integer)",
    "pipeline_notify_done(uuid, uuid)",
)
NEW_COLUMNS = ("withheld_reason", "work_lease_token", "work_lease_until", "work_attempts",
               "notify_done_at")  # fmt: skip


# --------------------------------------------------------------------------- данные


@dataclass
class P:
    user_id: uuid.UUID
    agent_id: uuid.UUID


async def _person(engine: AsyncEngine, name: str) -> P:
    uid, aid = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as c:
        await c.execute(
            text("INSERT INTO users (id, telegram_id, name) VALUES (:u, :tg, :n)"),
            {"u": uid, "tg": next(_tg), "n": name},
        )
        await c.execute(text(f"UPDATE users SET {ONBOARDED} WHERE id = :u"), {"u": uid})
        await c.execute(
            text("INSERT INTO agents (id, user_id) VALUES (:a, :u)"), {"a": aid, "u": uid}
        )
    return P(uid, aid)


async def _pair(sm: SM, engine: AsyncEngine) -> tuple[P, P, uuid.UUID]:
    """Анна и Игорь — активная пара через функции (АР-13)."""
    anna, igor = await _person(engine, "Анна"), await _person(engine, "Игорь")
    couple = uuid.UUID(str(await _call(sm, anna.user_id, "SELECT app_create_couple()")))
    token = _token()
    await _call(sm, anna.user_id, "SELECT app_issue_invite(:h)", h=token)
    await _call(sm, igor.user_id, ACCEPT, h=token)
    return anna, igor, couple


async def _send(
    sm: SM, engine: AsyncEngine, author: P, body: bytes, reply_to: uuid.UUID | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    """Черновик автора и «Отправить» (`app_send_draft`) → сообщение `processing`."""
    did = uuid.uuid4()
    async with engine.begin() as c:
        await c.execute(
            text(
                "INSERT INTO message_drafts (id, agent_id, text_enc, reply_to_id) "
                "VALUES (:d, :a, :t, :r)"
            ),
            {"d": did, "a": author.agent_id, "t": body, "r": reply_to},
        )
    mid = uuid.UUID(str(await _call(sm, author.user_id, SEND, d=did)))
    return mid, did


async def _park(engine: AsyncEngine) -> None:
    """Чужие строки обеих очередей — под долгой арендой: захват их не видит."""
    async with engine.begin() as c:
        await c.execute(
            text(
                "UPDATE partner_messages SET work_lease_token = gen_random_uuid(), "
                "work_lease_until = now() + interval '1 day' "
                "WHERE status = 'processing' "
                "   OR (status = 'delivered' AND notify_done_at IS NULL)"
            )
        )


@asynccontextmanager
async def _worker_tx(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(text("SET LOCAL ROLE feelings_worker"))
        yield conn


async def _w(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with _worker_tx(engine) as conn:
        return list(await conn.execute(text(sql), params))


async def _w1(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    rows = await _w(engine, sql, **params)
    assert len(rows) == 1
    return rows[0][0]


async def _wtry(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    """Результат вызова под feelings_worker или SQLSTATE (для гонок)."""
    try:
        return await _w1(engine, sql, **params)
    except DBAPIError as exc:
        return _sqlstate(exc)


async def _claim(engine: AsyncEngine, n: int = 3, lim: int = 100) -> dict[uuid.UUID, Any]:
    return {r.message_id: r for r in await _w(engine, CLAIM, l=LEASE, n=n, lim=lim)}


async def _deliver(
    engine: AsyncEngine, m: uuid.UUID, t: Any, body: bytes = b"for-recipient", **kw: Any
) -> Any:
    p = {"m": m, "t": t, "b": body, "ad": True, "tone": 2, "h": False, "ev": None} | kw
    return await _w1(engine, DELIVER, **p)


async def _msg(engine: AsyncEngine, m: uuid.UUID) -> dict[str, Any] | None:
    async with engine.connect() as c:
        row = (
            (
                await c.execute(
                    text(
                        "SELECT status::text AS status, withheld_reason, work_lease_token, "
                        "work_lease_until, work_attempts, notify_done_at, adapted_enc, is_adapted, "
                        "tone_score, recipient_id, delivered_at FROM partner_messages WHERE id = :m"
                    ),
                    {"m": m},
                )
            )
            .mappings()
            .one_or_none()
        )
    return None if row is None else dict(row)


async def _events(engine: AsyncEngine, uid: uuid.UUID) -> list[str]:
    async with engine.connect() as c:
        rows = await c.execute(
            text("SELECT type::text FROM safety_events WHERE user_id = :u ORDER BY type"),
            {"u": uid},
        )
        return [r[0] for r in rows]


async def _expire(engine: AsyncEngine, m: uuid.UUID) -> None:
    """Воркер «упал»: аренда истекла."""
    async with engine.begin() as c:
        await c.execute(
            text("UPDATE partner_messages SET work_lease_until = now() - interval '1 second' "
                 "WHERE id = :m"),
            {"m": m},
        )  # fmt: skip


async def _feed(sm: SM, uid: uuid.UUID) -> dict[uuid.UUID, Any]:
    async with user_scope(sm, uid) as s:
        rows = await s.execute(text("SELECT * FROM app_couple_feed_page(100, NULL, NULL)"))
        return {r.id: r for r in rows}


# --------------------------------------------------------------------------- права


async def test_privileges(engine: AsyncEngine) -> None:
    """Функции 0013 — только feelings_worker; новых колонок приложение не видит и не пишет;
    у воркера прав на таблицу нет (только функции)."""
    async with engine.connect() as conn:

        async def q(sql: str, **p: Any) -> Any:
            return (await conn.execute(text(sql), p)).scalar_one()

        for f in NEW_WORKER_FUNCS:
            for role in ("feelings_app", "feelings_api", "feelings_admin", "public"):
                assert not await q("SELECT has_function_privilege(:r, :f, 'EXECUTE')", r=role, f=f)
            assert await q("SELECT has_function_privilege('feelings_worker', :f, 'EXECUTE')", f=f)
            assert await q("SELECT prosecdef FROM pg_proc WHERE oid = to_regprocedure(:f)", f=f)
        for col in NEW_COLUMNS:
            for role in ("feelings_app", "feelings_worker", "feelings_admin"):
                for priv in ("SELECT", "INSERT", "UPDATE"):
                    assert not await q(
                        "SELECT has_column_privilege(:r, 'partner_messages', :c, :p)",
                        r=role,
                        c=col,
                        p=priv,
                    ), (role, col, priv)
        assert not await q(
            "SELECT has_table_privilege('feelings_worker', 'partner_messages', 'SELECT')"
        )


async def test_worker_has_no_table_access(engine: AsyncEngine) -> None:
    with pytest.raises(DBAPIError) as ei:
        await _w(engine, "SELECT original_enc FROM partner_messages LIMIT 1")
    assert _sqlstate(ei.value) == "42501"


# --------------------------------------------------------------------------- основной путь


async def test_claim_read_deliver_notify(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Отправка → захват → оригинал с draft_id (Б-39) → доставка по токену с событием только
    типом от имени автора → ответ «отвечено» → очередь уведомлений (Б-47) → отметка."""
    await _park(engine)
    anna, igor, _couple = await _pair(sessionmaker, engine)
    first, _ = await _send(sessionmaker, engine, anna, b"orig-1")
    got = await _claim(engine)
    assert set(got) == {first} and got[first].attempts == 1
    t1 = got[first].token
    assert await _deliver(engine, first, t1) == "delivered"

    reply, did = await _send(sessionmaker, engine, igor, b"orig-reply", reply_to=first)
    got = await _claim(engine)
    assert set(got) == {reply}
    tok = got[reply].token
    rows = await _w(engine, READ, m=reply, t=tok)
    assert [(bytes(r.original_enc), r.draft_id) for r in rows] == [(b"orig-reply", did)]
    assert await _claim(engine) == {}  # под арендой — второй раз не выдаётся
    out = await _deliver(engine, reply, tok, b"adapted", ev=["underage", "underage"])
    assert out == "delivered"
    row = await _msg(engine, reply)
    assert row is not None
    assert (
        row["status"],
        row["withheld_reason"],
        row["work_lease_token"],
        row["work_attempts"],
    ) == (
        "delivered",
        None,
        None,
        0,
    )
    assert bytes(row["adapted_enc"]) == b"adapted" and row["is_adapted"] and row["tone_score"] == 2
    assert (await _msg(engine, first) or {})["status"] == "answered"
    assert await _events(engine, igor.user_id) == ["underage", "underage"]
    assert await _events(engine, anna.user_id) == []
    assert await _w(engine, READ, m=reply, t=tok) == []  # после доставки — не читается
    assert await _deliver(engine, reply, tok) == "lost"  # одно одобрение — одна доставка

    # Очередь уведомлений (Б-47): id сообщения и получатель, без текста.
    rows = await _w(engine, CLAIM_NOTIFY, l=LEASE, n=3, lim=100)
    assert all(
        tuple(r._fields) == ("message_id", "recipient_id", "token", "attempts") for r in rows
    )
    notes = {r.message_id: r for r in rows}
    # Исходное уже «отвечено» (получатель ответил — видел его): уведомление не нужно.
    assert {m: r.recipient_id for m, r in notes.items()} == {reply: anna.user_id}
    assert await _w(engine, CLAIM_NOTIFY, l=LEASE, n=3, lim=100) == []
    assert await _w1(engine, NOTIFY_DONE, m=reply, t=uuid.uuid4()) is False
    # Прочитано раньше отметки уведомления — отметка всё равно ставится.
    assert await _call(sessionmaker, anna.user_id, "SELECT app_mark_read(:m)", m=reply) is True
    assert await _w1(engine, NOTIFY_DONE, m=reply, t=notes[reply].token) is True
    assert await _w1(engine, NOTIFY_DONE, m=reply, t=notes[reply].token) is False
    row = await _msg(engine, reply)
    assert row is not None and row["notify_done_at"] is not None
    assert row["work_lease_token"] is None and row["status"] == "read"


async def test_read_requires_lease_and_sent_draft(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Б-48: оригинал отдаётся только по своей аренде, пока сообщение в конвейере и черновик
    отправлен автором; иначе пусто — одобрение не выдаётся."""
    await _park(engine)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    mid, did = await _send(sessionmaker, engine, anna, b"orig")
    tok = (await _claim(engine))[mid].token
    for t in (None, uuid.uuid4()):
        assert await _w(engine, READ, m=mid, t=t) == []
    assert await _w(engine, READ, m=uuid.uuid4(), t=tok) == []
    async with engine.begin() as c:
        await c.execute(
            text("UPDATE message_drafts SET status = 'approved' WHERE id = :d"), {"d": did}
        )
    assert await _w(engine, READ, m=mid, t=tok) == []
    async with engine.begin() as c:
        await c.execute(text("UPDATE message_drafts SET status = 'sent' WHERE id = :d"), {"d": did})
    assert len(await _w(engine, READ, m=mid, t=tok)) == 1


async def test_withhold_reasons_visible_to_author_only(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Кризис (АР-28) и отказ модели (АР-27): не доставлено, причина — автору в ленте,
    получатель не видит сообщения; события — только тип, от имени автора, только при успехе."""
    await _park(engine)
    anna, igor, _ = await _pair(sessionmaker, engine)
    crisis, _ = await _send(sessionmaker, engine, anna, b"crisis")
    refused, _ = await _send(sessionmaker, engine, anna, b"refused")
    got = await _claim(engine)
    for bad in (None, "attempts_exhausted", "couple_ended", "nope"):
        with pytest.raises(DBAPIError) as ei:
            await _w(engine, WITHHOLD, m=crisis, t=got[crisis].token, r=bad, ev=None, cr=None)
        assert _sqlstate(ei.value) == "22023"
    assert (
        await _w1(
            engine, WITHHOLD, m=crisis, t=uuid.uuid4(), r="safety", ev=["violence"], cr="violence"
        )
        is False
    )
    assert await _events(engine, anna.user_id) == []
    assert (
        await _w1(
            engine,
            WITHHOLD,
            m=crisis,
            t=got[crisis].token,
            r="safety",
            ev=["threat_outgoing"],
            cr="threat_outgoing",
        )
        is True
    )
    assert (
        await _w1(
            engine, WITHHOLD, m=refused, t=got[refused].token, r="model_refusal", ev=[], cr=None
        )
        is True
    )
    assert await _events(engine, anna.user_id) == ["threat_outgoing"]
    assert await _events(engine, igor.user_id) == []
    feed = await _feed(sessionmaker, anna.user_id)
    assert (feed[crisis].status, feed[crisis].withheld_reason) == ("withheld", "safety")
    assert (feed[refused].status, feed[refused].withheld_reason) == ("withheld", "model_refusal")
    assert crisis not in await _feed(sessionmaker, igor.user_id)
    # Повторное завершение и доставка после остановки — ничего не меняют.
    assert await _deliver(engine, crisis, got[crisis].token) == "lost"
    assert (
        await _w1(
            engine,
            WITHHOLD,
            m=refused,
            t=got[refused].token,
            r="safety",
            ev=["violence"],
            cr="violence",
        )
        is False
    )
    assert await _events(engine, anna.user_id) == ["threat_outgoing"]


async def test_deliver_rejects_null_text(sessionmaker: SM, engine: AsyncEngine) -> None:
    await _park(engine)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    mid, _ = await _send(sessionmaker, engine, anna, b"orig")
    tok = (await _claim(engine))[mid].token
    with pytest.raises(DBAPIError) as ei:
        await _deliver(engine, mid, tok, None)  # type: ignore[arg-type]
    assert _sqlstate(ei.value) == "22023"
    assert (await _msg(engine, mid) or {})["status"] == "processing"


UNLEASED = (
    "SELECT pipeline_original(:m)",
    "SELECT pipeline_deliver(:m, 'x', false, 0::smallint, false)",
    "SELECT pipeline_withhold(:m)",
    "SELECT pipeline_withhold_leased(:m, :t, 'safety', ARRAY['violence']::safety_event_type[])",
)


async def test_unleased_functions_removed(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Б-83, 0016 (АР-48): `pipeline_original`/`pipeline_deliver`/`pipeline_withhold` (0001/0005,
    без аренды) и 4-аргументная обёртка `pipeline_withhold_leased` (0013) сняты — воркер их не
    вызовет (42883), сообщение остаётся в конвейере; путь по аренде работает. Взамен у ролей
    ничего не появилось — права функций конвейера сверяет `test_privileges` и
    `test_rls_a8.py::test_definer_functions_executable_only_by_intended_roles`."""
    await _park(engine)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    mid, _ = await _send(sessionmaker, engine, anna, b"orig")
    for sql in UNLEASED:
        with pytest.raises(DBAPIError) as ei:
            await _w(engine, sql, m=mid, t=uuid.uuid4())
        assert _sqlstate(ei.value) == "42883", sql
    row = await _msg(engine, mid)
    assert row is not None and (row["status"], row["work_lease_token"]) == ("processing", None)
    tok = (await _claim(engine))[mid].token
    assert await _deliver(engine, mid, tok) == "delivered"


async def test_reason_check_constraints(sessionmaker: SM, engine: AsyncEngine) -> None:
    anna, _igor, _ = await _pair(sessionmaker, engine)
    mid, _ = await _send(sessionmaker, engine, anna, b"orig")
    for sql in (
        "UPDATE partner_messages SET withheld_reason = 'safety' WHERE id = :m",
        "UPDATE partner_messages SET status = 'withheld', withheld_reason = 'x' WHERE id = :m",
        "UPDATE partner_messages SET work_lease_token = gen_random_uuid() WHERE id = :m",
        "UPDATE partner_messages SET work_attempts = -1 WHERE id = :m",
    ):
        with pytest.raises(DBAPIError) as ei:
            async with engine.begin() as c:
                await c.execute(text(sql), {"m": mid})
        assert _sqlstate(ei.value) == "23514", sql


# --------------------------------------------------------------------------- аренда и догон


async def test_lease_expiry_catch_up_and_stale_token(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Догон после перезапуска: истёкшая аренда выдаётся снова (новый токен, attempts + 1);
    старый токен ничего не завершает; истёкшая, но не перехваченная — завершается."""
    await _park(engine)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    mid, _ = await _send(sessionmaker, engine, anna, b"orig")
    first = (await _claim(engine))[mid]
    await _expire(engine, mid)
    second = (await _claim(engine))[mid]
    assert second.token != first.token and second.attempts == 2
    assert await _w(engine, READ, m=mid, t=first.token) == []
    assert await _deliver(engine, mid, first.token) == "lost"
    await _expire(engine, mid)  # истекла, но никто не перехватил
    assert await _deliver(engine, mid, second.token) == "delivered"


async def test_attempts_exhausted_withheld_for_author(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Предел попыток: следующий захват без модели ставит `withheld/attempts_exhausted` —
    понятный исход для автора, получатель ничего не видит."""
    await _park(engine)
    anna, igor, _ = await _pair(sessionmaker, engine)
    mid, _ = await _send(sessionmaker, engine, anna, b"orig")
    for n in (1, 2):
        assert (await _claim(engine, n=2))[mid].attempts == n
        await _expire(engine, mid)
    assert await _claim(engine, n=2) == {}
    row = await _msg(engine, mid)
    assert row is not None
    assert (row["status"], row["withheld_reason"], row["work_lease_token"]) == (
        "withheld",
        "attempts_exhausted",
        None,
    )
    assert (await _feed(sessionmaker, anna.user_id))[mid].withheld_reason == "attempts_exhausted"
    assert mid not in await _feed(sessionmaker, igor.user_id)
    assert await _events(engine, anna.user_id) == []


async def test_claim_argument_checks_and_limit(sessionmaker: SM, engine: AsyncEngine) -> None:
    await _park(engine)
    for fn in (CLAIM, CLAIM_NOTIFY):
        for lease, n in (("0 seconds", 3), ("2 days", 3), (None, 3), (LEASE, 0), (LEASE, 101),
                         (LEASE, None)):  # fmt: skip
            with pytest.raises(DBAPIError) as ei:
                await _w(engine, fn, l=lease, n=n, lim=5)
            assert _sqlstate(ei.value) == "22023", (fn, lease, n)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    mids = [(await _send(sessionmaker, engine, anna, b"m%d" % i))[0] for i in range(3)]
    async with engine.begin() as c:  # одинаковое время (моложе 24 ч, АР-45) — порядок по id
        await c.execute(
            text("UPDATE partner_messages "
                 "SET sent_at = date_trunc('minute', now()) - interval '1 hour' "
                 "WHERE id = ANY(:ids)"),
            {"ids": mids},
        )  # fmt: skip
    one = await _w(engine, CLAIM, l=LEASE, n=3, lim=0)
    assert [r.message_id for r in one] == [min(mids)]
    rest = await _w(engine, CLAIM, l=LEASE, n=3, lim=None)
    assert [r.message_id for r in rest] == [sorted(mids)[1]]


# --------------------------------------------------------------------------- пара распалась


async def test_couple_ended_before_claim_and_before_delivery(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """ТЗ §4.5 п.2: после выхода обмен прекращается. Распад до захвата — захват завершает
    `couple_ended` без модели; распад, пока шла модель, — доставка отвечает `couple_ended`."""
    await _park(engine)
    anna, igor, _ = await _pair(sessionmaker, engine)
    early, _ = await _send(sessionmaker, engine, anna, b"early")
    await _call(sessionmaker, igor.user_id, LEAVE)
    assert await _claim(engine) == {}
    assert (await _msg(engine, early) or {})["withheld_reason"] == "couple_ended"

    vera, oleg, _ = await _pair(sessionmaker, engine)
    late, _ = await _send(sessionmaker, engine, vera, b"late")
    tok = (await _claim(engine))[late].token
    await _call(sessionmaker, vera.user_id, LEAVE)
    assert await _deliver(engine, late, tok, ev=["underage"]) == "couple_ended"
    row = await _msg(engine, late)
    assert row is not None
    assert (row["status"], row["withheld_reason"], row["adapted_enc"]) == (
        "withheld",
        "couple_ended",
        None,
    )
    assert await _events(engine, vera.user_id) == ["underage"]
    assert await _events(engine, oleg.user_id) == []


async def test_notify_queue_skips_gone_recipient_and_dissolved(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Б-47: получатель удалён или пара распалась — уведомление не нужно (отметка без выдачи);
    исчерпаны попытки — тоже."""
    await _park(engine)
    anna, igor, _ = await _pair(sessionmaker, engine)
    to_igor, _ = await _send(sessionmaker, engine, anna, b"a")
    to_anna, _ = await _send(sessionmaker, engine, igor, b"b")
    got = await _claim(engine)
    assert await _deliver(engine, to_igor, got[to_igor].token) == "delivered"
    assert await _deliver(engine, to_anna, got[to_anna].token) == "delivered"
    vera, oleg, _ = await _pair(sessionmaker, engine)
    to_oleg, _ = await _send(sessionmaker, engine, vera, b"c")
    tok = (await _claim(engine))[to_oleg].token
    assert await _deliver(engine, to_oleg, tok) == "delivered"

    await _call(sessionmaker, igor.user_id, DELETE)  # пара Анны распущена, Игоря нет
    notes = await _w(engine, CLAIM_NOTIFY, l=LEASE, n=1, lim=100)
    assert [(r.message_id, r.recipient_id) for r in notes] == [(to_oleg, oleg.user_id)]
    for m in (to_anna, to_igor):
        row = await _msg(engine, m)
        assert row is None or row["notify_done_at"] is not None
    await _expire(engine, to_oleg)
    assert await _w(engine, CLAIM_NOTIFY, l=LEASE, n=1, lim=100) == []  # попытки исчерпаны
    assert (await _msg(engine, to_oleg) or {})["notify_done_at"] is not None


# --------------------------------------------------------------------------- гонки


async def test_two_workers_never_get_same_message(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Две копии воркера одновременно: выданные множества не пересекаются, всё выдано ровно раз;
    открытая транзакция захвата не задерживает вторую копию (SKIP LOCKED)."""
    await _park(engine)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    mids = {(await _send(sessionmaker, engine, anna, b"m%d" % i))[0] for i in range(10)}
    async with _worker_tx(engine) as held:
        first = {r.message_id for r in await held.execute(text(CLAIM), {"l": LEASE, "n": 3,
                                                                      "lim": 4})}  # fmt: skip
        async with _worker_tx(engine) as other:
            await other.execute(text("SET LOCAL lock_timeout = '1s'"))
            second = {r.message_id for r in await other.execute(text(CLAIM),
                                                                {"l": LEASE, "n": 3, "lim": 100})}  # fmt: skip
    assert len(first) == 4 and first | second == mids and not first & second
    for m in mids:
        await _expire(engine, m)
    res = await asyncio.gather(*(_claim(engine, n=5, lim=3) for _ in range(5)))
    ids = [m for r in res for m in r]
    assert sorted(ids) == sorted(mids)


async def test_parallel_deliveries_with_same_token_only_one_wins(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    await _park(engine)
    anna, igor, _ = await _pair(sessionmaker, engine)
    mid, _ = await _send(sessionmaker, engine, anna, b"orig")
    tok = (await _claim(engine))[mid].token
    res = await asyncio.gather(
        _deliver(engine, mid, tok, ev=["underage"]),
        _w1(engine, WITHHOLD, m=mid, t=tok, r="safety", ev=["violence"], cr="violence"),
        _deliver(engine, mid, tok, ev=["underage"]),
    )
    wins = [r for r in res if r in ("delivered", True)]
    assert len(wins) == 1
    assert len(await _events(engine, anna.user_id)) == 1
    assert await _events(engine, igor.user_id) == []


async def test_claim_does_not_wait_for_account_deletion(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Захват ничего не ждёт: пока удаление аккаунта автора не зафиксировано, его сообщения
    пропускаются (SKIP LOCKED), остальные выдаются; после — их нет."""
    await _park(engine)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    vera, _oleg, _ = await _pair(sessionmaker, engine)
    gone, _ = await _send(sessionmaker, engine, anna, b"gone")
    kept, _ = await _send(sessionmaker, engine, vera, b"kept")
    async with user_scope(sessionmaker, anna.user_id) as s:
        await s.execute(text(DELETE))
        async with _worker_tx(engine) as w:
            await w.execute(text("SET LOCAL lock_timeout = '1s'"))
            got = {r.message_id for r in await w.execute(text(CLAIM), {"l": LEASE, "n": 3,
                                                                       "lim": 100})}  # fmt: skip
        assert got == {kept}
    assert await _msg(engine, gone) is None


async def test_delete_waits_for_open_claim_transaction(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Транзакция захвата держит строки сообщений; удаление аккаунта дожидается её (не 40P01)."""
    await _park(engine)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    mid, _ = await _send(sessionmaker, engine, anna, b"orig")
    async with _worker_tx(engine) as w:
        got = {r.message_id for r in await w.execute(text(CLAIM), {"l": LEASE, "n": 3, "lim": 100})}
        assert got == {mid}
        task = asyncio.ensure_future(_try(sessionmaker, anna.user_id, DELETE))
        await _wait_lock_waiters(engine, "app_delete_account", 1)
    assert await asyncio.wait_for(task, 30) is None
    assert await _msg(engine, mid) is None


Step = tuple[str, Any]


async def _race(engine: AsyncEngine, lock_sql: str, params: dict[str, Any],
                steps: Sequence[Step]) -> dict[str, Any]:  # fmt: skip
    res = await _hold_and_race(engine, lock_sql, params, steps)
    return dict(zip([s[0] for s in steps], res, strict=True))


@pytest.mark.parametrize("first", ["delete", "deliver"])
@pytest.mark.parametrize("op", ["deliver", "withhold"])
async def test_race_author_deletes_account_vs_finish(
    sessionmaker: SM, engine: AsyncEngine, first: str, op: str
) -> None:
    """АР-13, Б-45: удаление аккаунта автора и завершение с событием безопасности (вставка
    safety_events берёт строку автора FOR KEY SHARE) — без 40P01 в обоих порядках. Удаление
    первым — завершение `lost`/false, событий нет; завершение первым — затем всё удаляется."""
    await _park(engine)
    anna, igor, _ = await _pair(sessionmaker, engine)
    earlier, _ = await _send(sessionmaker, engine, igor, b"earlier")
    t0 = (await _claim(engine))[earlier].token
    assert await _deliver(engine, earlier, t0) == "delivered"
    mid, _ = await _send(sessionmaker, engine, anna, b"reply", reply_to=earlier)
    tok = (await _claim(engine))[mid].token
    fn = "pipeline_deliver_leased" if op == "deliver" else "pipeline_withhold_leased"
    call = (
        _wtry(engine, DELIVER, m=mid, t=tok, b=b"x", ad=False, tone=0, h=False, ev=["underage"])
        if op == "deliver"
        else _wtry(engine, WITHHOLD, m=mid, t=tok, r="safety", ev=["self_harm"], cr="self_harm")
    )
    delete = ("app_delete_account", _try(sessionmaker, anna.user_id, DELETE))
    finish = (fn, call)
    steps = [delete, finish] if first == "delete" else [finish, delete]
    got = await _race(
        engine, "SELECT 1 FROM users WHERE id = :u FOR UPDATE", {"u": anna.user_id}, steps
    )
    ok = "delivered" if op == "deliver" else True
    lost = "lost" if op == "deliver" else False
    assert got == {"app_delete_account": None, fn: ok if first == "deliver" else lost}, got
    assert await _msg(engine, mid) is None
    assert await _events(engine, anna.user_id) == []
    row = await _msg(engine, earlier)
    assert row is not None and row["status"] == (
        "answered" if first == "deliver" and op == "deliver" else "delivered"
    )


@pytest.mark.parametrize("first", ["other", "deliver"])
@pytest.mark.parametrize("other", ["delete_recipient", "leave_recipient", "leave_author"])
async def test_race_recipient_gone_or_leave_vs_deliver(
    sessionmaker: SM, engine: AsyncEngine, first: str, other: str
) -> None:
    """Удаление аккаунта получателя и выход из пары (любым) против доставки ответа (ответ →
    исходное «отвечено»): без 40P01. Первым удаление/выход — `couple_ended`; первой доставка —
    доставлено, затем удаление/выход."""
    await _park(engine)
    anna, igor, couple = await _pair(sessionmaker, engine)
    earlier, _ = await _send(sessionmaker, engine, igor, b"earlier")
    t0 = (await _claim(engine))[earlier].token
    assert await _deliver(engine, earlier, t0) == "delivered"
    mid, _ = await _send(sessionmaker, engine, anna, b"reply", reply_to=earlier)
    tok = (await _claim(engine))[mid].token
    who = anna if other == "leave_author" else igor
    sql, fname = (DELETE, "app_delete_account") if other == "delete_recipient" else (
        LEAVE, "app_leave_couple")  # fmt: skip
    rival = (fname, _try(sessionmaker, who.user_id, sql))
    deliver = (
        "pipeline_deliver_leased",
        _wtry(engine, DELIVER, m=mid, t=tok, b=b"x", ad=True, tone=3, h=False, ev=["underage"]),
    )
    steps = [rival, deliver] if first == "other" else [deliver, rival]
    got = await _race(
        engine, "SELECT 1 FROM couples WHERE id = :c FOR UPDATE", {"c": couple}, steps
    )
    assert got["pipeline_deliver_leased"] == ("couple_ended" if first == "other" else "delivered")
    assert got[fname] not in ("40P01", "23503"), got
    row = await _msg(engine, mid)
    assert row is not None
    if first == "other":
        assert (row["status"], row["withheld_reason"]) == ("withheld", "couple_ended")
    else:
        assert row["status"] == "delivered"
    assert await _events(engine, anna.user_id) == ["underage"]


async def test_send_draft_not_blocked_by_open_delivery(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Открытая транзакция доставки (автор и пара — FOR KEY SHARE) не задерживает новую
    отправку того же автора и встречную отправку партнёра (АР-13)."""
    await _park(engine)
    anna, igor, _ = await _pair(sessionmaker, engine)
    mid, _ = await _send(sessionmaker, engine, anna, b"orig")
    tok = (await _claim(engine))[mid].token
    async with _worker_tx(engine) as w:
        res = await w.execute(
            text(DELIVER),
            {"m": mid, "t": tok, "b": b"x", "ad": False, "tone": 0, "h": False, "ev": ["underage"]},
        )
        assert res.scalar_one() == "delivered"
        for who in (anna, igor):
            await asyncio.wait_for(_send(sessionmaker, engine, who, b"next"), 5)


# --------------------------------------------------------------------------- миграция


async def test_downgrade_0012_upgrade_backfill_check(
    sessionmaker: SM, engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """0013 → 0012 → head: downgrade убирает функции, колонки, индексы, возвращает тела
    `pipeline_withhold` (0005) и `app_couple_feed_page` (0012); upgrade помечает прежние
    `withheld` причиной `safety`, прежние доставленные — уведомлёнными; `alembic check` чист."""
    alembic("downgrade", "0012")
    await engine.dispose()
    await app_engine.dispose()
    try:
        async with engine.connect() as conn:
            n = await conn.execute(
                text("SELECT count(*) FROM pg_proc WHERE proname = ANY(:n)"),
                {"n": [f.split("(")[0] for f in NEW_WORKER_FUNCS]},
            )
            assert n.scalar_one() == 0
            cols = await conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'partner_messages' AND column_name = ANY(:c)"
                ),
                {"c": list(NEW_COLUMNS)},
            )
            assert cols.scalar_one() == 0
            body = await conn.execute(
                text("SELECT pg_get_functiondef('pipeline_withhold(uuid)'::regprocedure)")
            )
            assert "withheld_reason" not in body.scalar_one()
            res = await conn.execute(
                text(
                    "SELECT pg_get_function_result('app_couple_feed_page(integer, timestamp with "
                    "time zone, uuid)'::regprocedure)"
                )
            )
            assert "withheld_reason" not in res.scalar_one()
        anna, _igor, _ = await _pair(sessionmaker, engine)
        held, _ = await _send(sessionmaker, engine, anna, b"held")
        done, _ = await _send(sessionmaker, engine, anna, b"done")
        await _w1(engine, "SELECT pipeline_withhold(:m)", m=held)
        await _w1(engine, "SELECT pipeline_deliver(:m, 'x', false, 0::smallint, false)", m=done)
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    alembic("check")
    assert (await _msg(engine, held) or {})["withheld_reason"] == "safety"
    row = await _msg(engine, done)
    assert row is not None and row["notify_done_at"] == row["delivered_at"]
    assert (await _feed(sessionmaker, anna.user_id))[held].withheld_reason == "safety"


# --------------------------------------------------------------------------- 0014 (раунд 1, АР-45)

RELEASE = "SELECT pipeline_release(:m, :t, CAST(CAST(:r AS text) AS interval), CAST(:c AS boolean))"


async def _released(engine: AsyncEngine, m: uuid.UUID) -> tuple[Any, Any, Any]:
    row = await _msg(engine, m)
    assert row is not None
    return row["work_lease_token"], row["work_lease_until"], row["work_attempts"]


async def test_release_retry_after_and_attempt_not_counted(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """АР-45 п.2: досрочное снятие аренды по своему токену; попытка не расходуется при
    `p_count_attempt = false`; `p_retry_after` — не выдавать раньше срока; старый токен — `lost`."""
    await _park(engine)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    mid, _ = await _send(sessionmaker, engine, anna, b"orig")
    first = (await _claim(engine))[mid]
    assert await _w1(engine, RELEASE, m=mid, t=uuid.uuid4(), r=None, c=True) is False
    assert await _w1(engine, RELEASE, m=mid, t=first.token, r=None, c=False) is True
    assert await _released(engine, mid) == (None, None, 0)
    assert await _w1(engine, RELEASE, m=mid, t=first.token, r=None, c=False) is False
    assert await _deliver(engine, mid, first.token) == "lost"
    second = (await _claim(engine))[mid]  # сразу снова в очереди, попытка — снова первая
    assert second.attempts == 1 and second.token != first.token
    assert await _w1(engine, RELEASE, m=mid, t=second.token, r="1 hour", c=True) is True
    token, until, attempts = await _released(engine, mid)
    assert token is None and until is not None and attempts == 1
    assert await _claim(engine) == {}  # «не раньше» срока
    await _expire(engine, mid)
    third = (await _claim(engine))[mid]
    assert third.attempts == 2
    assert await _deliver(engine, mid, third.token) == "delivered"
    assert (
        await _w1(engine, RELEASE, m=mid, t=third.token, r=None, c=True) is False
    )  # не processing
    for r, c in (("-1 second", True), ("2 days", True), (None, None)):
        with pytest.raises(DBAPIError) as ei:
            await _w(engine, RELEASE, m=mid, t=third.token, r=r, c=c)
        assert _sqlstate(ei.value) == "22023", (r, c)


async def test_release_does_not_wait_for_couple_or_user_locks(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Снятие аренды меняет одну строку сообщения: удаление/выход, держащие пару и пользователя
    FOR UPDATE, его не задерживают (АР-13)."""
    await _park(engine)
    anna, _igor, couple = await _pair(sessionmaker, engine)
    mid, _ = await _send(sessionmaker, engine, anna, b"orig")
    tok = (await _claim(engine))[mid].token
    async with engine.connect() as holder, holder.begin():
        await holder.execute(text("SELECT 1 FROM couples WHERE id = :c FOR UPDATE"), {"c": couple})
        await holder.execute(
            text("SELECT 1 FROM users WHERE id = :u FOR UPDATE"), {"u": anna.user_id}
        )
        async with _worker_tx(engine) as w:
            await w.execute(text("SET LOCAL lock_timeout = '1s'"))
            res = await w.execute(text(RELEASE), {"m": mid, "t": tok, "r": "5 seconds", "c": False})
            assert res.scalar_one() is True


async def test_wait_limit_24h_from_sent(sessionmaker: SM, engine: AsyncEngine) -> None:
    """АР-45: сообщение ждёт дольше 24 ч от отправки (пауза воркера) — при захвате
    `attempts_exhausted` без модели, даже если попытки не расходовались."""
    await _park(engine)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    old, _ = await _send(sessionmaker, engine, anna, b"old")
    fresh, _ = await _send(sessionmaker, engine, anna, b"fresh")
    async with engine.begin() as c:
        await c.execute(
            text(
                "UPDATE partner_messages SET sent_at = now() - interval '24 hours 1 minute' "
                "WHERE id = :m"
            ),
            {"m": old},
        )
    assert set(await _claim(engine, n=100)) == {fresh}
    row = await _msg(engine, old)
    assert row is not None
    assert (row["status"], row["withheld_reason"], row["work_attempts"]) == (
        "withheld",
        "attempts_exhausted",
        0,
    )


async def test_crisis_type_kept_for_author_only(sessionmaker: SM, engine: AsyncEngine) -> None:
    """АР-45 п.3 (ревью MAJOR-3): тип кризиса хранится у сообщения и отдаётся только автору —
    экран выберет протокол (шаблон отправителя при `threat_outgoing`, §9.3, АР-28)."""
    await _park(engine)
    anna, igor, _ = await _pair(sessionmaker, engine)
    threat, _ = await _send(sessionmaker, engine, anna, b"threat")
    harm, _ = await _send(sessionmaker, engine, anna, b"harm")
    refused, _ = await _send(sessionmaker, engine, anna, b"refused")
    got = await _claim(engine)
    bad = (
        ("safety", None),
        ("safety", "underage"),
        ("model_refusal", "violence"),
        ("pipeline_error", "self_harm"),
    )
    for reason, crisis in bad:
        with pytest.raises(DBAPIError) as ei:
            await _w(engine, WITHHOLD, m=threat, t=got[threat].token, r=reason, ev=None, cr=crisis)
        assert _sqlstate(ei.value) == "22023", (reason, crisis)
    assert (
        await _w1(
            engine,
            WITHHOLD,
            m=threat,
            t=got[threat].token,
            r="safety",
            ev=["threat_outgoing"],
            cr="threat_outgoing",
        )
        is True
    )
    # Главный тип — не «младше 18», хотя `underage` в событиях первым (тип выбирает воркер —
    # `pipeline_work.crisis_type`; обёртка 0013, выводившая его в БД, снята в 0016).
    assert (
        await _w1(
            engine,
            WITHHOLD,
            m=harm,
            t=got[harm].token,
            r="safety",
            ev=["underage", "self_harm"],
            cr="self_harm",
        )
        is True
    )
    assert (
        await _w1(
            engine, WITHHOLD, m=refused, t=got[refused].token, r="model_refusal", ev=None, cr=None
        )
        is True
    )
    feed = await _feed(sessionmaker, anna.user_id)
    assert (feed[threat].withheld_reason, feed[threat].withheld_crisis) == (
        "safety",
        "threat_outgoing",
    )
    assert (feed[harm].withheld_reason, feed[harm].withheld_crisis) == ("safety", "self_harm")
    assert (feed[refused].withheld_reason, feed[refused].withheld_crisis) == (
        "model_refusal",
        None,
    )
    igor_feed = await _feed(sessionmaker, igor.user_id)
    assert not {threat, harm, refused} & set(igor_feed)
    assert all(r.withheld_crisis is None and r.withheld_reason is None for r in igor_feed.values())
    assert await _events(engine, anna.user_id) == ["self_harm", "threat_outgoing", "underage"]
    for sql in (
        "UPDATE partner_messages SET withheld_crisis = 'violence' WHERE id = :m",  # не safety
        "UPDATE partner_messages SET withheld_reason = 'safety', withheld_crisis = 'underage' "
        "WHERE id = :m",
    ):
        with pytest.raises(DBAPIError) as ei:
            async with engine.begin() as c:
                await c.execute(text(sql), {"m": refused})
        assert _sqlstate(ei.value) == "23514", sql


async def test_crisis_after_couple_ended_stays_safety(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """О-3: кризис, остановленный после распада пары, — `safety` с типом (помощь автору важнее),
    не `couple_ended`; событие — от автора."""
    await _park(engine)
    anna, igor, _ = await _pair(sessionmaker, engine)
    mid, _ = await _send(sessionmaker, engine, anna, b"crisis")
    tok = (await _claim(engine))[mid].token
    await _call(sessionmaker, igor.user_id, LEAVE)
    assert (
        await _w1(engine, WITHHOLD, m=mid, t=tok, r="safety", ev=["life_threat"], cr="life_threat")
        is True
    )
    row = await _msg(engine, mid)
    assert row is not None
    assert (row["status"], row["withheld_reason"]) == ("withheld", "safety")
    async with engine.connect() as c:
        crisis = await c.execute(
            text("SELECT withheld_crisis::text FROM partner_messages WHERE id = :m"), {"m": mid}
        )
        assert crisis.scalar_one() == "life_threat"
    assert await _events(engine, anna.user_id) == ["life_threat"]


async def test_finish_retry_after_failed_write_is_idempotent(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """D1 (на уровне БД): запись исхода, которая не зафиксировалась (обрыв), повторяется тем же
    токеном и проходит; повтор после успешной — `false` без изменений и без вторых событий.
    Пока аренда не истекла, сообщение никому не выдаётся; истёкшая, но не перехваченная —
    повтор записи всё равно проходит."""
    await _park(engine)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    mid, _ = await _send(sessionmaker, engine, anna, b"refused")
    tok = (await _claim(engine))[mid].token
    async with engine.connect() as conn:
        tx = await conn.begin()
        await conn.execute(text("SET LOCAL ROLE feelings_worker"))
        res = await conn.execute(
            text(WITHHOLD), {"m": mid, "t": tok, "r": "model_refusal", "ev": None, "cr": None}
        )
        assert res.scalar_one() is True
        await tx.rollback()  # «соединение оборвалось до фиксации»
    assert await _claim(engine) == {}  # аренда жива — не выдаётся
    await _expire(engine, mid)
    assert (
        await _w1(engine, WITHHOLD, m=mid, t=tok, r="model_refusal", ev=["underage"], cr=None)
        is True
    )
    assert (
        await _w1(engine, WITHHOLD, m=mid, t=tok, r="model_refusal", ev=["underage"], cr=None)
        is False
    )
    row = await _msg(engine, mid)
    assert row is not None and (row["status"], row["withheld_reason"]) == (
        "withheld",
        "model_refusal",
    )
    assert await _events(engine, anna.user_id) == ["underage"]
    assert await _claim(engine) == {}


async def test_leased_message_not_finished_by_other_token(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """MINOR-8, Б-83: сообщение, которое держит воркер, никто другой не читает и не завершает —
    функций без аренды больше нет (0016), а чужой токен даёт пусто / `lost` / false."""
    await _park(engine)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    mid, _ = await _send(sessionmaker, engine, anna, b"orig")
    tok = (await _claim(engine))[mid].token
    other = uuid.uuid4()
    assert await _w(engine, READ, m=mid, t=other) == []
    assert await _deliver(engine, mid, other) == "lost"
    assert (
        await _w1(engine, WITHHOLD, m=mid, t=other, r="safety", ev=["violence"], cr="violence")
        is False
    )
    assert await _events(engine, anna.user_id) == []
    assert (await _msg(engine, mid) or {})["status"] == "processing"
    assert await _deliver(engine, mid, tok) == "delivered"


async def test_downgrade_0013_upgrade_check(
    sessionmaker: SM, engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """0014 → 0013 → head: отложенный срок без токена сбрасывается (CHECK 0013 «токен ⇔ срок»),
    тип кризиса и функции 0014 удаляются, тела 0013 возвращаются; `alembic check` чист."""
    await _park(engine)
    anna, _igor, _ = await _pair(sessionmaker, engine)
    mid, _ = await _send(sessionmaker, engine, anna, b"orig")
    held, _ = await _send(sessionmaker, engine, anna, b"held")
    got = await _claim(engine)
    assert await _w1(engine, RELEASE, m=mid, t=got[mid].token, r="1 hour", c=False) is True
    assert await _w1(
        engine, WITHHOLD, m=held, t=got[held].token, r="safety", ev=None, cr="violence"
    )
    alembic("downgrade", "0013")
    await engine.dispose()
    await app_engine.dispose()
    try:
        async with engine.connect() as conn:
            n = await conn.execute(
                text(
                    "SELECT count(*) FROM pg_proc WHERE proname = 'pipeline_release' "
                    "OR (proname = 'pipeline_withhold_leased' AND pronargs = 5)"
                )
            )
            assert n.scalar_one() == 0
            col = await conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'partner_messages' AND column_name = 'withheld_crisis'"
                )
            )
            assert col.scalar_one() == 0
            body = await conn.execute(
                text("SELECT pg_get_functiondef('pipeline_original(uuid)'::regprocedure)")
            )
            assert "work_lease_token" not in body.scalar_one()
        assert await _released(engine, mid) == (None, None, 0)
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    alembic("check")


# --------------------------------------------------------------------------- 0016 (Б-83, АР-48)

REMOVED_0016 = (
    "pipeline_original(uuid)",
    "pipeline_deliver(uuid, bytea, boolean, smallint, boolean)",
    "pipeline_withhold(uuid)",
    "pipeline_withhold_leased(uuid, uuid, character varying, safety_event_type[])",
)


async def _removed_state(engine: AsyncEngine) -> dict[str, Any]:
    """Сигнатура → (определение, ACL, SECURITY DEFINER, настройки, владелец) или None."""
    out: dict[str, Any] = {}
    async with engine.connect() as conn:
        for sig in REMOVED_0016:
            row = (
                await conn.execute(
                    text(
                        "SELECT pg_get_functiondef(p.oid) AS def, p.proacl::text AS acl, "
                        "p.prosecdef, p.proconfig, pg_get_userbyid(p.proowner) AS owner "
                        "FROM pg_proc p WHERE p.oid = to_regprocedure(:f)"
                    ),
                    {"f": sig},
                )
            ).one_or_none()
            out[sig] = None if row is None else tuple(row)
    return out


async def test_migration_0016_reversible_restores_functions_exactly(
    engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """0016: upgrade снимает 4 функции без аренды; downgrade восстанавливает их точно как было
    до 0016 (тело 0014, ACL — только владелец и `feelings_worker`, SECURITY DEFINER,
    `search_path`, владелец); снова upgrade — их нет; `alembic check` чист. Эталон «как было» —
    состояние, собранное настоящими миграциями до 0015 (через 0013 → 0015)."""
    assert set((await _removed_state(engine)).values()) == {None}
    try:
        alembic("downgrade", "0013")
        alembic("upgrade", "0015")
        await engine.dispose()
        before = await _removed_state(engine)
        assert None not in before.values()
        for sig, (_body, acl, definer, cfg, _owner) in before.items():
            assert definer is True, sig
            assert any(c.startswith("search_path=") for c in cfg or []), sig
            assert "feelings_worker=X/" in acl and not acl.startswith("{=X"), (sig, acl)
            assert ",=X/" not in acl, (sig, acl)  # PUBLIC без EXECUTE
        alembic("upgrade", "0016")
        await engine.dispose()
        assert set((await _removed_state(engine)).values()) == {None}
        alembic("downgrade", "0015")
        await engine.dispose()
        assert await _removed_state(engine) == before
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    alembic("check")
    assert set((await _removed_state(engine)).values()) == {None}

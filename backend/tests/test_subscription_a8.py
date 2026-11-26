"""4.4, этап 1 — негативные сценарии тестировщика (A8) к миграции 0017: подписка пары, пробный
период, счета и платежи Telegram Stars.

ФТ-19; ТЗ §4 (роль «управляет подпиской»), §7 (``subscriptions``, ``payments``, правила доступа),
§9.1 п.5 (журналы без текстов и личных данных), §11 п.1–7; DECISIONS.md АР-13, АР-23, АР-38,
АР-50. Данные выдуманные, платежи — заглушки (выдуманные ``telegram_payment_charge_id``), ни
одного обращения к Telegram (CLAUDE.md). Помощники — из ``test_subscription_db.py`` (A1).

Дефекты отмечены ``xfail(strict=True)`` с пометкой ``A8-4.4-Dn`` (docs/reviews/4.4-s1-test-r1.md).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import user_scope
from tests.conftest import BACKEND_DIR, TEST_DATABASE_URL, alembic
from tests.test_account_export_db import HEAD
from tests.test_couple_functions import (
    ACCEPT,
    LEAVE,
    SM,
    _call,
    _fails,
    _sqlstate,
    _wait_lock_waiters,
)
from tests.test_subscription_db import (
    BOT_FUNCS,
    CREDIT,
    DELETE,
    HAS_ACCESS,
    INTERNAL_FUNCS,
    INVOICE,
    LOOKUP,
    MONTH,
    PRECHECK,
    REFUNDED,
    STATUS,
    TRIAL,
    USER_FUNCS,
    _charge,
    _couple,
    _credit,
    _invoice,
    _one,
    _rows,
    _su,
    _tg_id,
)

YEAR = {"plan": "year", "amount": 2000, "days": 365, "ttl": timedelta(hours=1)}
# Коды, которые гонки вправе вернуть; 40P01/40001 и прочее — провал (АР-13, АР-38).
RACE_OK = {"FC001", "FB007", "42501"}


def _alembic_heads() -> list[str]:
    out = subprocess.run(  # noqa: S603 — фиксированная команда
        [sys.executable, "-m", "alembic", "-x", f"db_url={TEST_DATABASE_URL}", "heads"],
        cwd=BACKEND_DIR,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.split()


async def _subs_count(engine: AsyncEngine, couple: uuid.UUID) -> int:
    return int(
        await _su(engine, "SELECT count(*) FROM subscriptions WHERE couple_id = :c", c=couple)
    )


async def _precheck(sm: SM, inv: uuid.UUID, tg: int, amount: int = 250, cur: Any = "XTR") -> str:
    return str(await _call(sm, None, PRECHECK, i=inv, tg=tg, amount=amount, cur=cur))


def _codes(results: list[Any]) -> list[str | None]:
    """SQLSTATE исключений из gather(return_exceptions=True); прочие исключения — как есть."""
    out: list[str | None] = []
    for r in results:
        if isinstance(r, DBAPIError):
            out.append(_sqlstate(r))
        elif isinstance(r, BaseException):
            raise r
    return out


# --------------------------------------------------------------------------- двойное зачисление


async def test_repeated_delivery_credits_once(sessionmaker: SM, engine: AsyncEngine) -> None:
    """§11, АР-50: пять доставок одного successful_payment подряд — один ``credited``, остальные
    ``duplicate``; один период, один успешный платёж, одна запись audit_log."""
    a, _b, couple = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    charge = _charge()
    res = [await _credit(sessionmaker, engine, inv, a, charge) for _ in range(5)]
    assert [r["result"] for r in res] == ["credited"] + ["duplicate"] * 4
    assert len({(r["payment_id"], r["subscription_id"], r["period_end"]) for r in res}) == 1
    assert await _subs_count(engine, couple) == 1
    assert (
        await _su(
            engine,
            "SELECT count(*) FROM payments WHERE couple_id = :c AND status = 'succeeded'",
            c=couple,
        )
        == 1
    )
    assert (
        await _su(
            engine,
            "SELECT count(*) FROM audit_log WHERE action = 'payment.credit' AND object_id = :i",
            i=inv,
        )
        == 1
    )


@pytest.mark.parametrize("held", [True, False], ids=["couple-held", "free-run"])
async def test_parallel_delivery_two_connections(
    sessionmaker: SM, engine: AsyncEngine, held: bool
) -> None:
    """Одновременные доставки одного платежа разными соединениями: ровно один ``credited``, без
    ошибок (23505 — значит, перепроверки после блокировки пары нет). ``couple-held`` — оба
    проходят первую проверку и ждут пару; ``free-run`` — четыре вызова без удержания."""
    a, _b, couple = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    tg = await _tg_id(engine, a)
    charge = _charge()

    def call() -> asyncio.Task[dict[str, Any]]:
        return asyncio.create_task(
            _one(sessionmaker, None, CREDIT, i=inv, tg=tg, amount=250, cur="XTR", ch=charge)
        )

    if held:
        async with engine.connect() as holder:
            tx = await holder.begin()
            await holder.execute(
                text("SELECT 1 FROM couples WHERE id = :c FOR UPDATE"), {"c": couple}
            )
            calls = [call(), call()]
            await _wait_lock_waiters(engine, "app_bot_payment_credit", 2)
            await tx.rollback()
    else:
        calls = [call() for _ in range(4)]
    res = await asyncio.gather(*calls)
    results = sorted(r["result"] for r in res)
    assert results == ["credited"] + ["duplicate"] * (len(res) - 1)
    assert len({r["subscription_id"] for r in res}) == 1
    assert await _subs_count(engine, couple) == 1


async def test_same_charge_for_other_invoice_is_duplicate(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Тот же ``telegram_payment_charge_id`` для другого счёта (своей или чужой пары) —
    ``duplicate`` с данными первого платежа: второй счёт не оплачивается, периода не появляется."""
    a, _b, couple = await _couple(sessionmaker, engine)
    x, _y, other = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    own_second = await _invoice(sessionmaker, a, **YEAR)
    foreign = await _invoice(sessionmaker, x)
    charge = _charge()
    first = await _credit(sessionmaker, engine, inv, a, charge)
    assert first["result"] == "credited"
    r1 = await _one(
        sessionmaker, None, CREDIT, i=own_second, tg=await _tg_id(engine, a), amount=2000,
        cur="XTR", ch=charge,
    )  # fmt: skip
    r2 = await _credit(sessionmaker, engine, foreign, x, charge)
    for r in (r1, r2):
        assert r == {**first, "result": "duplicate"}
    assert await _subs_count(engine, couple) == 1
    assert await _subs_count(engine, other) == 0
    for i in (own_second, foreign):
        assert await _su(engine, "SELECT status FROM payments WHERE id = :i", i=i) == "pending"


async def test_same_charge_two_couples_in_parallel_one_period(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Одновременно один charge_id на счета двух разных пар (блокировки пар разные): период
    появляется ровно у одной пары. Проигравший — ``duplicate`` или 23505 уникальности
    (повтор бота даёт ``duplicate``) — инвариант держит уникальный ``provider_charge_id``."""
    a, _b, c1 = await _couple(sessionmaker, engine)
    x, _y, c2 = await _couple(sessionmaker, engine)
    i1, i2 = await _invoice(sessionmaker, a), await _invoice(sessionmaker, x)
    t1, t2 = await _tg_id(engine, a), await _tg_id(engine, x)
    charge = _charge()
    async with engine.connect() as holder:
        tx = await holder.begin()
        await holder.execute(
            text("SELECT 1 FROM couples WHERE id IN (:a, :b) FOR UPDATE"), {"a": c1, "b": c2}
        )
        calls = [
            asyncio.create_task(
                _one(sessionmaker, None, CREDIT, i=i, tg=t, amount=250, cur="XTR", ch=charge)
            )
            for i, t in ((i1, t1), (i2, t2))
        ]
        await _wait_lock_waiters(engine, "app_bot_payment_credit", 2)
        await tx.rollback()
    res = await asyncio.gather(*calls, return_exceptions=True)
    ok = [r for r in res if isinstance(r, dict)]
    assert [r["result"] for r in ok].count("credited") == 1
    assert all(r["result"] in ("credited", "duplicate") for r in ok)
    assert all(c == "23505" for c in _codes(list(res)))
    assert await _subs_count(engine, c1) + await _subs_count(engine, c2) == 1
    retry = await _one(sessionmaker, None, CREDIT, i=i2, tg=t2, amount=250, cur="XTR", ch=charge)
    assert retry["result"] == "duplicate"
    assert (
        await _su(engine, "SELECT count(*) FROM payments WHERE provider_charge_id = :ch", ch=charge)
        == 1
    )


# --------------------------------------------------------------------------- сумма, тариф, срок


async def test_amount_and_currency_mismatch_change_nothing(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """§11 п.8, Д6: сумма и валюта сверяются со счётом; несовпадение — ни периода, ни платежа,
    ни записи audit_log; тариф и срок берутся из счёта, а не из обновления."""
    a, b, couple = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a, **YEAR)
    tg = await _tg_id(engine, b)
    for amount, cur in ((1999, "XTR"), (2001, "XTR"), (2000, "xtr"), (2000, "USD"), (2000, None)):
        assert await _precheck(sessionmaker, inv, tg, amount, cur) == "amount_mismatch"
        r = await _one(
            sessionmaker, None, CREDIT, i=inv, tg=tg, amount=amount, cur=cur, ch=_charge()
        )
        assert (r["result"], r["payment_id"], r["subscription_id"]) == (
            "amount_mismatch", None, None,
        )  # fmt: skip
    assert await _subs_count(engine, couple) == 0
    assert await _su(engine, "SELECT status FROM payments WHERE id = :i", i=inv) == "pending"
    assert await _su(engine, "SELECT count(*) FROM audit_log WHERE object_id = :i", i=inv) == 0
    before = datetime.now(UTC)
    ok = await _one(sessionmaker, None, CREDIT, i=inv, tg=tg, amount=2000, cur="XTR", ch=_charge())
    assert ok["result"] == "credited"
    row = await _su(
        engine,
        "SELECT plan || '|' || extract(epoch FROM period_end - period_start)::bigint "
        "FROM subscriptions WHERE id = :s",
        s=ok["subscription_id"],
    )
    assert row == f"year|{365 * 86400}"
    assert ok["period_end"] >= before + timedelta(days=365)


async def test_invoice_terms_are_not_mixed(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Переиспользуется только счёт с теми же тарифом, суммой и сроком той же пары; счёт другой
    пары с теми же условиями — свой (§7, АР-50)."""
    a, b, _c = await _couple(sessionmaker, engine)
    x, _y, _c2 = await _couple(sessionmaker, engine)
    base = await _one(sessionmaker, a, INVOICE, **MONTH)
    for kw in ({"days": 31}, {"plan": "year"}, {"amount": 251}):
        r = await _one(sessionmaker, b, INVOICE, **{**MONTH, **kw})
        assert r["reused"] is False and r["invoice_id"] != base["invoice_id"], kw
    foreign = await _one(sessionmaker, x, INVOICE, **MONTH)
    assert foreign["reused"] is False and foreign["invoice_id"] != base["invoice_id"]
    # Короткий остаток срока (меньше половины) — новый счёт, а не почти истёкший.
    await _su(
        engine,
        "UPDATE payments SET expires_at = now() + interval '20 minutes' WHERE id = :i",
        i=base["invoice_id"],
    )
    fresh = await _one(sessionmaker, a, INVOICE, **MONTH)
    assert fresh["reused"] is False and fresh["invoice_id"] != base["invoice_id"]


# --------------------------------------------------------------------------- чужая пара, распад


async def test_foreign_invoice_gives_stranger_nothing(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """§11 п.1, §7: счёт пары X оплачивает участник пары Y. pre_checkout — ``not_member``; если
    Telegram всё же зачислил (деньги списаны) — период получает пара X, плательщик Y ни периода,
    ни доступа, ни видимости строк X не получает."""
    a, b, cx = await _couple(sessionmaker, engine)
    y, z, cy = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    tg_y = await _tg_id(engine, y)
    assert await _precheck(sessionmaker, inv, tg_y) == "not_member"
    r = await _credit(sessionmaker, engine, inv, y, _charge())
    assert r["result"] == "credited"
    assert await _subs_count(engine, cy) == 0
    for who in (y, z):
        assert await _call(sessionmaker, who, HAS_ACCESS) is False
        st = await _one(sessionmaker, who, STATUS)
        assert (st["couple_id"], st["status"], st["paid_by_me"]) == (cy, "none", None)
        async with user_scope(sessionmaker, who) as s:
            for t in ("subscriptions", "payments"):
                assert (await s.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one() == 0
    for who in (a, b):
        st = await _one(sessionmaker, who, STATUS)
        assert (st["couple_id"], st["status"], st["paid_by_me"]) == (cx, "active", False)


async def test_invoice_after_dissolution_and_payer_deletion(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """§11 п.6, 4.5 (АР-23): после распада новый счёт — FC001 обоим, pre_checkout —
    ``couple_not_active``; оплата, начатая до распада, — ``couple_ended``. После удаления
    аккаунта плательщика его Telegram ID больше не узнаётся: плательщик NULL, audit_log без
    actor, lookup без Telegram ID."""
    a, b, couple = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    late = await _invoice(sessionmaker, b, amount=99)
    tg_a = await _tg_id(engine, a)
    await _call(sessionmaker, b, LEAVE)
    for who in (a, b):
        await _fails(sessionmaker, who, "FC001", INVOICE, **MONTH)
        await _fails(sessionmaker, who, "FC001", TRIAL, d=7)
    assert await _precheck(sessionmaker, inv, tg_a) == "couple_not_active"
    await _call(sessionmaker, a, DELETE)
    assert await _precheck(sessionmaker, inv, tg_a) == "couple_not_active"
    charge = _charge()
    r = await _one(sessionmaker, None, CREDIT, i=late, tg=tg_a, amount=99, cur="XTR", ch=charge)
    assert r["result"] == "couple_ended"
    assert (
        await _su(
            engine, "SELECT payer_id FROM subscriptions WHERE id = :s", s=r["subscription_id"]
        )
        is None
    )
    assert (
        await _su(
            engine,
            "SELECT count(*) FROM audit_log WHERE object_id = :p AND actor_id IS NULL",
            p=r["payment_id"],
        )
        == 1
    )
    assert (await _one(sessionmaker, None, LOOKUP, ch=charge))["payer_telegram_id"] is None
    assert await _call(sessionmaker, b, HAS_ACCESS) is True  # оплаченный период дослуживает
    assert await _subs_count(engine, couple) == 1


async def test_payer_deleted_while_couple_active_then_partner(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Плательщик удаляет аккаунт: партнёр сохраняет оплаченный доступ (пара распалась, §11
    п.6), функции пользователя для удалённого — 42501; удаление партнёра стирает пару с
    периодами и платежами (АР-23 (а)), и повторная доставка платежа его не восстанавливает."""
    a, b, couple = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    charge = _charge()
    await _credit(sessionmaker, engine, inv, a, charge)
    await _call(sessionmaker, a, DELETE)
    for sql, params in ((STATUS, {}), (HAS_ACCESS, {}), (TRIAL, {"d": 7}), (INVOICE, MONTH)):
        await _fails(sessionmaker, a, "42501", sql, **params)
    assert await _call(sessionmaker, b, HAS_ACCESS) is True
    await _call(sessionmaker, b, DELETE)
    assert await _subs_count(engine, couple) == 0
    r = await _one(sessionmaker, None, CREDIT, i=inv, tg=1, amount=250, cur="XTR", ch=charge)
    assert r["result"] == "invoice_not_found"
    assert await _rows(sessionmaker, None, LOOKUP, ch=charge) == []
    assert (await _one(sessionmaker, None, REFUNDED, ch=charge))["result"] == "not_found"


# --------------------------------------------------------------------------- истёкший счёт


async def test_expired_invoice(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Истёкший счёт: pre_checkout — ``invoice_expired``, переиспользования нет; платёж, прошедший
    pre_checkout до истечения, всё равно зачисляется (деньги списаны). Истёкший меньше суток —
    не удаляется; больше суток и не оплачен — удаляется, оплаченный — нет."""
    a, b, couple = await _couple(sessionmaker, engine)
    tg = await _tg_id(engine, b)
    inv = await _invoice(sessionmaker, a)
    paid_old = await _invoice(sessionmaker, a, amount=300)
    await _credit(sessionmaker, engine, paid_old, b, _charge(), amount=300)
    await _su(
        engine,
        "UPDATE payments SET expires_at = now() - interval '1 millisecond' WHERE id = :i",
        i=inv,
    )
    assert await _precheck(sessionmaker, inv, tg) == "invoice_expired"
    again = await _one(sessionmaker, a, INVOICE, **MONTH)
    assert again["reused"] is False and again["invoice_id"] != inv
    assert await _su(engine, "SELECT count(*) FROM payments WHERE id = :i", i=inv) == 1
    r = await _credit(sessionmaker, engine, inv, b, _charge())
    assert r["result"] == "credited"
    stale = await _invoice(sessionmaker, a, amount=400)
    await _su(
        engine,
        "UPDATE payments SET expires_at = now() - interval '25 hours' WHERE id IN (:s, :p)",
        s=stale,
        p=paid_old,
    )
    await _one(sessionmaker, b, INVOICE, **{**MONTH, "amount": 401})
    assert await _su(engine, "SELECT count(*) FROM payments WHERE id = :s", s=stale) == 0
    assert await _su(engine, "SELECT status FROM payments WHERE id = :p", p=paid_old) == "succeeded"
    assert await _subs_count(engine, couple) == 2


async def test_cleanup_touches_only_own_couple(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Чистка истёкших счетов при выпуске нового — только своей пары."""
    a, _b, _c1 = await _couple(sessionmaker, engine)
    x, _y, c2 = await _couple(sessionmaker, engine)
    foreign = await _invoice(sessionmaker, x)
    await _su(
        engine,
        "UPDATE payments SET expires_at = now() - interval '3 days' WHERE couple_id = :c",
        c=c2,
    )
    await _invoice(sessionmaker, a)
    assert await _su(engine, "SELECT count(*) FROM payments WHERE id = :i", i=foreign) == 1


# --------------------------------------------------------------------------- предел счетов


async def test_open_invoice_limit_edges(sessionmaker: SM, engine: AsyncEngine) -> None:
    """FC002 (→ 429): 20-й действующий счёт выпускается, 21-й — нет; переиспользование при
    пределе работает; истёкшие (меньше суток) в предел не входят; предел — на пару."""
    a, b, couple = await _couple(sessionmaker, engine)
    x, _y, _c2 = await _couple(sessionmaker, engine)
    ids = [await _invoice(sessionmaker, a, amount=100 + n) for n in range(20)]
    assert len(set(ids)) == 20
    await _fails(sessionmaker, b, "FC002", INVOICE, **{**MONTH, "amount": 999})
    reused = await _one(sessionmaker, b, INVOICE, **{**MONTH, "amount": 100})
    assert reused == {**reused, "invoice_id": ids[0], "reused": True}
    assert (await _one(sessionmaker, x, INVOICE, **MONTH))["reused"] is False
    await _su(
        engine,
        "UPDATE payments SET expires_at = now() - interval '1 hour' WHERE id = :i",
        i=ids[-1],
    )
    assert (await _one(sessionmaker, a, INVOICE, **{**MONTH, "amount": 999}))["reused"] is False
    await _fails(sessionmaker, a, "FC002", INVOICE, **{**MONTH, "amount": 998})
    assert (
        await _su(
            engine,
            "SELECT count(*) FROM payments WHERE couple_id = :c AND expires_at > now()",
            c=couple,
        )
        == 20
    )


# --------------------------------------------------------------------------- возврат


async def test_refund_twice_in_parallel_and_unknown(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Два одновременных возврата одного платежа — ``refunded`` и ``already_refunded``, одна
    запись audit_log; несуществующий — ``not_found``; пустой/длинный/NULL — 22023. Повторная
    доставка successful_payment после возврата — ``duplicate``, период остаётся отменённым."""
    a, b, couple = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    charge = _charge()
    paid = await _credit(sessionmaker, engine, inv, a, charge)
    async with engine.connect() as holder:
        tx = await holder.begin()
        await holder.execute(text("SELECT 1 FROM couples WHERE id = :c FOR UPDATE"), {"c": couple})
        calls = [
            asyncio.create_task(_one(sessionmaker, None, REFUNDED, ch=charge)) for _ in range(2)
        ]
        await _wait_lock_waiters(engine, "app_bot_payment_refunded", 2)
        await tx.rollback()
    res = await asyncio.gather(*calls)
    assert sorted(r["result"] for r in res) == ["already_refunded", "refunded"]
    assert {r["payment_id"] for r in res} == {inv}
    assert (
        await _su(
            engine,
            "SELECT count(*) FROM audit_log WHERE action = 'payment.refund' AND object_id = :i",
            i=inv,
        )
        == 1
    )
    for ch in (_charge(), "x" * 128):
        assert await _one(sessionmaker, None, REFUNDED, ch=ch) == {
            "result": "not_found", "payment_id": None,
        }  # fmt: skip
    for bad in ("", "x" * 129, None):
        await _fails(sessionmaker, None, "22023", REFUNDED, ch=bad)
    dup = await _credit(sessionmaker, engine, inv, a, charge)
    assert (dup["result"], dup["subscription_id"]) == ("duplicate", paid["subscription_id"])
    assert (
        await _su(
            engine, "SELECT status FROM subscriptions WHERE id = :s", s=paid["subscription_id"]
        )
        == "canceled"
    )
    for who in (a, b):
        assert await _call(sessionmaker, who, HAS_ACCESS) is False
    look = await _one(sessionmaker, None, LOOKUP, ch=charge)
    assert look["status"] == "refunded"


async def test_refund_of_last_period_keeps_earlier(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Возврат последнего из двух подряд оплаченных периодов: первый действует, доступ до его
    конца; следующий платёж встаёт сразу после первого (отменённый не учитывается)."""
    a, _b, _c = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    p1 = await _credit(sessionmaker, engine, inv, a, _charge())
    ch2 = _charge()
    await _credit(sessionmaker, engine, inv, a, ch2)
    await _one(sessionmaker, None, REFUNDED, ch=ch2)
    st = await _one(sessionmaker, a, STATUS)
    assert (st["status"], st["period_end"], st["has_access"]) == ("active", p1["period_end"], True)
    p3 = await _credit(sessionmaker, engine, inv, a, _charge())
    assert p3["period_end"] == p1["period_end"] + timedelta(days=30)


# A8-4.4-D1 исправлен в 0019 (4.4 р1, A1): xfail снят, тело не менялось.
async def test_payment_in_refund_gap_gives_access_now(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Периоды P1 [now, +30] и P2 [+30, +60]; возврат P1 (АР-50 п.4: следующие не сдвигаются).
    Ожидание: человек, оплативший снова, получает доступ сразу — новый платёж не должен
    вставать после P2, оставляя пару без доступа на 30 дней при двух оплаченных периодах."""
    a, _b, _c = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    ch1 = _charge()
    await _credit(sessionmaker, engine, inv, a, ch1)
    await _credit(sessionmaker, engine, inv, a, _charge())
    await _one(sessionmaker, None, REFUNDED, ch=ch1)
    await _credit(sessionmaker, engine, inv, a, _charge())
    assert await _call(sessionmaker, a, HAS_ACCESS) is True


# --------------------------------------------------------------------------- пробный период


async def test_trial_parallel_burst_without_holder(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Восемь одновременных вызовов обоих партнёров без удержания пары — одна строка, ни одного
    23505 (блокировка пары, а не только индекс)."""
    a, b, couple = await _couple(sessionmaker, engine)
    res = await asyncio.gather(
        *(_one(sessionmaker, u, TRIAL, d=7) for u in (a, b) * 4), return_exceptions=True
    )
    assert _codes(list(res)) == []
    ok = [r for r in res if isinstance(r, dict)]
    assert sorted(r["created"] for r in ok) == [False] * 7 + [True]
    assert len({r["subscription_id"] for r in ok}) == 1
    assert await _subs_count(engine, couple) == 1


async def test_trial_after_dissolution_and_new_couple(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """АР-50 п.6 (на подтверждении): после распада пробный старой пары — FC001 и доступа не даёт;
    новая пара — новый пробный период, в том числе если сошлись те же двое (фиксирует текущее
    допущение «один на пару»)."""
    a, b, old = await _couple(sessionmaker, engine)
    t_old = await _one(sessionmaker, a, TRIAL, d=7)
    await _call(sessionmaker, a, LEAVE)
    for who in (a, b):
        await _fails(sessionmaker, who, "FC001", TRIAL, d=7)
        assert await _call(sessionmaker, who, HAS_ACCESS) is False
    new = await _call(sessionmaker, a, "SELECT app_create_couple()")
    token = uuid.uuid4().bytes + uuid.uuid4().bytes
    await _call(sessionmaker, a, "SELECT app_issue_invite(:h)", h=token)
    await _call(sessionmaker, b, ACCEPT, h=token)
    t_new = await _one(sessionmaker, b, TRIAL, d=7)
    assert t_new["created"] is True and t_new["subscription_id"] != t_old["subscription_id"]
    assert await _subs_count(engine, old) == 1
    assert await _subs_count(engine, uuid.UUID(str(new))) == 1
    st = await _one(sessionmaker, a, STATUS)
    assert (st["status"], st["couple_id"]) == ("trial", uuid.UUID(str(new)))


# --------------------------------------------------------------------------- контексты и роли


async def test_bot_functions_after_switching_user_id(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Функции бота в сессии пользователя: смена ``app.user_id`` на другого пользователя или на
    не-uuid внутри транзакции — по-прежнему отказ (42501), ничего не зачислено.

    4.4 р1 (A1): с 0019 EXECUTE функций бота у ``feelings_app`` нет — отказ 42501 до разбора
    ``app.user_id``, поэтому и для не-uuid ожидается 42501 (было 22P02)."""
    a, b, couple = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    tg = await _tg_id(engine, a)
    for value, code in ((str(b), "42501"), (str(uuid.uuid4()), "42501"), ("garbage", "42501")):
        with pytest.raises(DBAPIError) as ei:
            async with user_scope(sessionmaker, a) as s:
                await s.execute(text("SELECT set_config('app.user_id', :v, true)"), {"v": value})
                await s.execute(
                    text(CREDIT),
                    {"i": inv, "tg": tg, "amount": 250, "cur": "XTR", "ch": _charge()},
                )
        assert _sqlstate(ei.value) == code, value
    assert await _subs_count(engine, couple) == 0


async def test_reset_user_id_reaches_service_context(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Предел защиты контекстом (АР-50 п.7, замечание A8-4.4-D2): код в сессии пользователя,
    сделавший ``RESET app.user_id`` (или ``set_config(…, '')``), в служебный контекст бота **не**
    попадает. Транзакция откатывается — ничего не зачисляется.

    4.4 р1 (A1): перевёрнут по смыслу — с 0019 функции бота исполняет только роль
    ``feelings_billing``, у ``feelings_app`` EXECUTE нет: вместо ``ok`` ожидается 42501 (D2)."""
    a, _b, couple = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    tg = await _tg_id(engine, a)
    for reset in ("RESET app.user_id", "SELECT set_config('app.user_id', '', true)"):
        async with sessionmaker() as session:
            tx = await session.begin()
            await session.execute(text("SET LOCAL ROLE feelings_app"))
            await session.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(a)})
            await session.execute(text(reset))
            with pytest.raises(DBAPIError) as ei:
                await session.execute(
                    text(PRECHECK), {"i": inv, "tg": tg, "amount": 250, "cur": "XTR"}
                )
            assert _sqlstate(ei.value) == "42501", reset
            await tx.rollback()
    assert await _subs_count(engine, couple) == 0


@pytest.mark.parametrize("role", ["feelings_worker", "feelings_admin", "feelings_api"])
async def test_login_role_cannot_switch_to_other_roles(sessionmaker: SM, role: str) -> None:
    """Роль входа API (NOINHERIT) без ``SET ROLE feelings_app`` функций 0017 не вызывает; на
    ``feelings_worker``/``feelings_admin`` переключиться не может (не член ролей)."""
    async with sessionmaker() as session:
        await session.begin()
        if role != "feelings_api":
            with pytest.raises(DBAPIError) as ei:
                await session.execute(text(f"SET LOCAL ROLE {role}"))
            assert _sqlstate(ei.value) == "42501"
            return
        await session.execute(text("SET LOCAL ROLE feelings_api"))
        with pytest.raises(DBAPIError) as ei:
            await session.execute(text("SELECT app_has_access()"))
        assert _sqlstate(ei.value) == "42501"


@pytest.mark.parametrize("role", ["feelings_worker", "feelings_admin", "feelings_api"])
async def test_other_roles_cannot_execute_any_0017_function(engine: AsyncEngine, role: str) -> None:
    """Под ролями worker/admin/api (SET ROLE от суперпользователя, откат) любой вызов функций
    0017 и прямой SELECT подписок/платежей — 42501."""
    calls = {
        "app_subscription_status()": "SELECT * FROM app_subscription_status()",
        "app_has_access()": "SELECT app_has_access()",
        "app_subscription_start_trial(integer)": "SELECT * FROM app_subscription_start_trial(7)",
        "app_subscription_create_invoice(character varying, integer, integer, interval)": (
            "SELECT * FROM app_subscription_create_invoice('month', 1, 30, interval '1 hour')"
        ),
        "app_bot_payment_precheck(uuid, bigint, integer, character varying)": (
            "SELECT app_bot_payment_precheck(gen_random_uuid(), 1, 1, 'XTR')"
        ),
        "app_bot_payment_credit(uuid, bigint, integer, character varying, character varying)": (
            "SELECT * FROM app_bot_payment_credit(gen_random_uuid(), 1, 1, 'XTR', 'x')"
        ),
        "app_bot_payment_refunded(character varying)": (
            "SELECT * FROM app_bot_payment_refunded('x')"
        ),
        "app_bot_payment_lookup(character varying)": "SELECT * FROM app_bot_payment_lookup('x')",
        "subscription_covered_until(uuid, timestamp with time zone)": (
            "SELECT subscription_covered_until(gen_random_uuid(), now())"
        ),
        "subscription_user_has_access(uuid)": (
            "SELECT subscription_user_has_access(gen_random_uuid())"
        ),
    }
    assert set(calls) == set(USER_FUNCS + BOT_FUNCS + INTERNAL_FUNCS)
    for sql in [*calls.values(), "SELECT 1 FROM subscriptions", "SELECT 1 FROM payments"]:
        async with engine.connect() as c:
            tx = await c.begin()
            await c.execute(text(f"SET LOCAL ROLE {role}"))
            with pytest.raises(DBAPIError) as ei:
                await c.execute(text(sql))
            assert _sqlstate(ei.value) == "42501", (role, sql)
            await tx.rollback()


async def test_internal_functions_not_callable_by_app(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Служебные функции без SECURITY DEFINER: ``feelings_app`` (и пользователь, и бот) их не
    вызывает — доступ чужой пары через них не проверить."""
    a, _b, _c = await _couple(sessionmaker, engine)
    for uid in (a, None):
        await _fails(sessionmaker, uid, "42501", "SELECT subscription_user_has_access(:u)", u=a)
        await _fails(
            sessionmaker, uid, "42501", "SELECT subscription_covered_until(:u, now())", u=a
        )


async def test_deleted_marked_user_has_no_user_functions(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Пользователь с ``deleted_at`` (удаление в процессе) — 42501 во всех функциях пользователя."""
    a, _b, _c = await _couple(sessionmaker, engine)
    await _su(engine, "UPDATE users SET deleted_at = now() WHERE id = :u", u=a)
    for sql, params in ((STATUS, {}), (HAS_ACCESS, {}), (TRIAL, {"d": 7}), (INVOICE, MONTH)):
        await _fails(sessionmaker, a, "42501", sql, **params)


# --------------------------------------------------------------------------- RLS


async def test_rls_other_couple_and_service_context(sessionmaker: SM, engine: AsyncEngine) -> None:
    """§7, §9.2: две оплаченные пары — каждый видит только свою подписку и платежи; в служебном
    контексте бота (``app.user_id`` пуст) прямой SELECT не видит ничего."""
    a, b, c1 = await _couple(sessionmaker, engine)
    x, y, c2 = await _couple(sessionmaker, engine)
    i1, i2 = await _invoice(sessionmaker, a), await _invoice(sessionmaker, x)
    await _credit(sessionmaker, engine, i1, b, _charge())
    await _credit(sessionmaker, engine, i2, y, _charge())
    for members, own, own_inv in (((a, b), c1, i1), ((x, y), c2, i2)):
        for who in members:
            async with user_scope(sessionmaker, who) as s:
                subs = (await s.execute(text("SELECT couple_id FROM subscriptions"))).scalars()
                assert set(subs) == {own}
                pays = (await s.execute(text("SELECT id FROM payments"))).scalars().all()
                assert pays == [own_inv]
    async with user_scope(sessionmaker, None) as s:
        for t in ("subscriptions", "payments"):
            assert (await s.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one() == 0


# --------------------------------------------------------------------------- граничные сроки


async def test_consecutive_payments_extend_from_end(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Оплата подряд продлевает от конца цепочки, а не от now(): периоды встык, статус до конца
    последнего; после истечения новая оплата начинается с now()."""
    a, b, couple = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    yinv = await _invoice(sessionmaker, b, **YEAR)
    p1 = await _credit(sessionmaker, engine, inv, a, _charge())
    p2 = await _credit(sessionmaker, engine, yinv, b, _charge(), amount=2000)
    p3 = await _credit(sessionmaker, engine, inv, b, _charge())
    assert p2["period_end"] == p1["period_end"] + timedelta(days=365)
    assert p3["period_end"] == p2["period_end"] + timedelta(days=30)
    starts = await _su(
        engine,
        "SELECT string_agg((period_start = lag_end)::text, ',' ORDER BY period_start) FROM "
        "(SELECT period_start, lag(period_end) OVER (ORDER BY period_start) AS lag_end "
        "FROM subscriptions WHERE couple_id = :c) t WHERE lag_end IS NOT NULL",
        c=couple,
    )
    assert starts == "true,true"
    st = await _one(sessionmaker, a, STATUS)
    assert (st["status"], st["plan"], st["period_end"]) == ("active", "month", p3["period_end"])
    assert st["paid_by_me"] is False and (await _one(sessionmaker, b, STATUS))["paid_by_me"]
    # Все периоды в прошлом — новая оплата от now(), а не от старого конца.
    await _su(
        engine,
        "UPDATE subscriptions SET period_start = period_start - interval '800 days', "
        "period_end = period_end - interval '800 days' WHERE couple_id = :c",
        c=couple,
    )
    assert (await _one(sessionmaker, a, STATUS))["status"] == "expired"
    before = datetime.now(UTC)
    p4 = await _credit(sessionmaker, engine, inv, a, _charge())
    after = datetime.now(UTC)
    assert before + timedelta(days=30) <= p4["period_end"] <= after + timedelta(days=30, seconds=5)


async def test_trial_boundary_and_paid_after_trial(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Пробный период, кончившийся секунду назад, — доступа нет; оплата после — от now(); пробный,
    кончающийся через минуту, — оплата встаёт после него."""
    a, _b, couple = await _couple(sessionmaker, engine)
    await _su(
        engine,
        "UPDATE couples SET activated_at = now() - interval '7 days' + interval '1 minute' "
        "WHERE id = :c",
        c=couple,
    )
    trial = await _one(sessionmaker, a, TRIAL, d=7)
    assert await _call(sessionmaker, a, HAS_ACCESS) is True
    inv = await _invoice(sessionmaker, a)
    p = await _credit(sessionmaker, engine, inv, a, _charge())
    assert p["period_end"] == trial["period_end"] + timedelta(days=30)
    x, _y, c2 = await _couple(sessionmaker, engine)
    await _su(
        engine,
        "UPDATE couples SET activated_at = now() - interval '7 days' - interval '1 second' "
        "WHERE id = :c",
        c=c2,
    )
    await _one(sessionmaker, x, TRIAL, d=7)
    assert await _call(sessionmaker, x, HAS_ACCESS) is False
    before = datetime.now(UTC)
    p2 = await _credit(sessionmaker, engine, await _invoice(sessionmaker, x), x, _charge())
    assert p2["period_end"] >= before + timedelta(days=30)


# --------------------------------------------------------------------------- журнал


async def test_audit_and_payments_hold_no_texts_or_telegram_ids(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """§9.1 п.5, §7: после пробного периода, счёта, зачисления, возврата и удаления аккаунта в
    ``audit_log`` и ``payments`` нет Telegram ID и имён; audit_log — только коды и id;
    колонки ``payments`` — без личных данных."""
    a, b, couple = await _couple(sessionmaker, engine)
    tgs = [str(await _tg_id(engine, u)) for u in (a, b)]
    names = ("Анна", "Игорь")
    trial = await _one(sessionmaker, a, TRIAL, d=7)
    inv = await _invoice(sessionmaker, a)
    charge = _charge()
    paid = await _credit(sessionmaker, engine, inv, b, charge)
    await _one(sessionmaker, None, REFUNDED, ch=charge)
    await _credit(sessionmaker, engine, inv, b, _charge())
    cols = await _su(
        engine,
        "SELECT string_agg(column_name, ',' ORDER BY column_name) FROM information_schema.columns "
        "WHERE table_name = 'payments'",
    )
    assert cols == (
        "amount,couple_id,created_at,currency,expires_at,id,paid_at,period_days,plan,provider,"
        "provider_charge_id,refunded_at,status,subscription_id"
    )
    audit_cols = await _su(
        engine,
        "SELECT string_agg(column_name, ',' ORDER BY column_name) FROM information_schema.columns "
        "WHERE table_name = 'audit_log'",
    )
    assert audit_cols == "action,actor_id,created_at,id,object_id,object_type"
    rows = await _su(
        engine,
        "SELECT string_agg(action || '|' || object_type, ',' ORDER BY id) FROM audit_log "
        "WHERE object_id IN (:t, :p) OR object_id IN (SELECT id FROM payments WHERE couple_id = :c)",
        t=trial["subscription_id"],
        p=paid["payment_id"],
        c=couple,
    )
    assert rows == (
        "subscription.trial|subscription,payment.credit|payment,payment.refund|payment,"
        "payment.credit|payment"
    )
    await _call(sessionmaker, b, DELETE)
    dump = await _su(
        engine,
        "SELECT coalesce((SELECT string_agg(row_to_json(l)::text, '') FROM audit_log l), '') || "
        "coalesce((SELECT string_agg(row_to_json(p)::text, '') FROM payments p), '')",
    )
    for needle in (*tgs, *names):
        assert needle not in dump, needle
    assert await _su(engine, "SELECT count(*) FROM audit_log WHERE actor_id = :b", b=b) == 0


# --------------------------------------------------------------------------- гонки 4.5


async def test_races_with_leave_and_delete_have_no_deadlocks(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """АР-13, АР-38: пробный период, счёт, зачисление, возврат одновременно с выходом и
    удалением аккаунтов обоих партнёров — без 40P01/40001; допустимы только отказы по
    состоянию (FC001, FB007, 42501)."""
    for round_ in range(6):
        a, b, couple = await _couple(sessionmaker, engine)
        inv = await _invoice(sessionmaker, a)
        old = _charge()
        await _credit(sessionmaker, engine, inv, a, old)
        tg_a, tg_b = await _tg_id(engine, a), await _tg_id(engine, b)
        ops: list[Coroutine[Any, Any, object]] = [
            _one(sessionmaker, None, CREDIT, i=inv, tg=tg_a, amount=250, cur="XTR", ch=_charge()),
            _one(sessionmaker, None, CREDIT, i=inv, tg=tg_b, amount=250, cur="XTR", ch=_charge()),
            _one(sessionmaker, None, REFUNDED, ch=old),
            _one(sessionmaker, a, TRIAL, d=7),
            _one(sessionmaker, b, INVOICE, **{**MONTH, "amount": 300 + round_}),
        ]
        ops.append(_call(sessionmaker, b, LEAVE if round_ % 2 else DELETE))
        if round_ >= 3:
            ops.append(_call(sessionmaker, a, DELETE))
        res = await asyncio.gather(*ops, return_exceptions=True)
        codes = _codes(list(res))
        assert set(codes) <= RACE_OK, (round_, codes)
        for r in res[:2]:
            if isinstance(r, dict):
                assert r["result"] in ("credited", "couple_ended", "invoice_not_found"), r
        status = await _su(
            engine, "SELECT coalesce((SELECT status::text FROM couples WHERE id = :c), 'gone')",
            c=couple,
        )  # fmt: skip
        assert status in ("dissolved", "gone"), status


async def test_refund_waits_for_account_deletion_without_deadlock(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Возврат (пара → платёж) ждёт удаление аккаунта партнёра (пользователь → пара) и
    проходит после него: ``refunded``, без 40P01."""
    a, b, couple = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    charge = _charge()
    await _credit(sessionmaker, engine, inv, a, charge)
    async with engine.connect() as holder:
        tx = await holder.begin()
        await holder.execute(text("SELECT 1 FROM couples WHERE id = :c FOR UPDATE"), {"c": couple})
        deleting = asyncio.create_task(_call(sessionmaker, b, DELETE))
        await _wait_lock_waiters(engine, "app_delete_account", 1)
        refunding = asyncio.create_task(_one(sessionmaker, None, REFUNDED, ch=charge))
        await _wait_lock_waiters(engine, "app_bot_payment_refunded", 1)
        await tx.rollback()
    await deleting
    assert (await refunding)["result"] == "refunded"


async def test_trial_waits_for_leave_and_sees_dissolution(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Пробный период, ждавший выхода партнёра, перепроверяет пару после блокировки — FC001,
    строки не появляется."""
    a, b, couple = await _couple(sessionmaker, engine)
    async with engine.connect() as holder:
        tx = await holder.begin()
        await holder.execute(text("SELECT 1 FROM couples WHERE id = :c FOR UPDATE"), {"c": couple})
        leaving = asyncio.create_task(_call(sessionmaker, b, LEAVE))
        await _wait_lock_waiters(engine, "app_leave_couple", 1)
        trial = asyncio.create_task(_one(sessionmaker, a, TRIAL, d=7))
        await _wait_lock_waiters(engine, "app_subscription_start_trial", 1)
        await tx.rollback()
    await leaving
    with pytest.raises(DBAPIError) as ei:
        await trial
    assert _sqlstate(ei.value) == "FC001"
    assert await _subs_count(engine, couple) == 0


# --------------------------------------------------------------------------- обратимость


async def test_migration_roundtrip_with_refunds_and_single_head(
    sessionmaker: SM, engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """0017 → 0016 → head с возвращённым платежом и пробным периодом: успешные и возвращённые
    платежи остаются, ожидающие счета удаляются; после upgrade функции работают (возврат
    по charge_id, новый счёт), ``alembic check`` чист, голова одна — ``HEAD`` (сейчас 0017)."""
    a, _b, couple = await _couple(sessionmaker, engine)
    await _one(sessionmaker, a, TRIAL, d=7)
    inv = await _invoice(sessionmaker, a)
    ch1, ch2 = _charge(), _charge()
    await _credit(sessionmaker, engine, inv, a, ch1)
    await _credit(sessionmaker, engine, inv, a, ch2)
    await _one(sessionmaker, None, REFUNDED, ch=ch1)
    await _invoice(sessionmaker, a, amount=55)
    try:
        alembic("downgrade", "0016")
        await engine.dispose()
        async with engine.connect() as c:
            n = (
                await c.execute(
                    text(
                        "SELECT string_agg(p.status::text, ',' ORDER BY p.status) FROM payments p "
                        "JOIN subscriptions s ON s.id = p.subscription_id WHERE s.couple_id = :c"
                    ),
                    {"c": couple},
                )
            ).scalar()
            assert n == "succeeded,refunded"
        alembic("upgrade", "head")
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    assert (await _one(sessionmaker, None, REFUNDED, ch=ch1))["result"] == "already_refunded"
    assert (await _one(sessionmaker, None, REFUNDED, ch=ch2))["result"] == "refunded"
    assert (await _one(sessionmaker, a, INVOICE, **MONTH))["reused"] is False
    alembic("check")
    heads = _alembic_heads()
    assert heads[:2] == [HEAD, "(head)"] and heads.count("(head)") == 1

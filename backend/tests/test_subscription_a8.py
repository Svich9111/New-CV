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
from tests._billing import alembic_on, migration_module
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


# =========================================================================== раунд 2 (0019)
#
# 4.4 э.1 р.2 (A8): роль платежей ``feelings_billing`` (АР-50 п.7; A8-4.4-D2, ревью MINOR-2),
# начало периода после возврата (АР-50 п.4; A8-4.4-D1), статус после распада (ревью MINOR-3),
# предел счетов под блокировкой (ревью MINOR-5). Роли кластера общие (CLAUDE.md): всё, что меняет
# роли и права, — внутри откатываемой транзакции суперпользователя; временные функции —
# в тестовой БД, удаляются в ``finally``.

R2_BOT_CALLS = {
    "precheck": "SELECT app_bot_payment_precheck(:i, :tg, 250, 'XTR')",
    "credit": "SELECT * FROM app_bot_payment_credit(:i, :tg, 250, 'XTR', :ch)",
    "refunded": "SELECT * FROM app_bot_payment_refunded(:ch)",
    "lookup": "SELECT * FROM app_bot_payment_lookup(:ch)",
}
R2_ROLES = ("feelings_app", "feelings_api", "feelings_worker", "feelings_admin")
ROLE_ATTRS = ("LOGIN", "INHERIT", "SUPERUSER", "CREATEROLE", "CREATEDB", "REPLICATION", "BYPASSRLS")


async def _r2_params(sm: SM, engine: AsyncEngine) -> tuple[uuid.UUID, dict[str, Any]]:
    a, _b, couple = await _couple(sm, engine)
    inv = await _invoice(sm, a)
    return couple, {"i": inv, "tg": await _tg_id(engine, a), "ch": _charge()}


def _r2_bind(sql: str, params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if f":{k}" in sql}


async def _r2_nothing_paid(engine: AsyncEngine, couple: uuid.UUID) -> None:
    assert await _subs_count(engine, couple) == 0
    assert (
        await _su(
            engine,
            "SELECT count(*) FROM payments WHERE couple_id = :c AND status <> 'pending'",
            c=couple,
        )
        == 0
    )


@pytest.mark.parametrize("role", R2_ROLES)
async def test_r2_superuser_set_role_other_roles_cannot_call_bot(
    sessionmaker: SM, engine: AsyncEngine, role: str
) -> None:
    """Под каждой ролью приложения (SET ROLE от суперпользователя, откат) все четыре функции бота —
    42501, в том числе с пустым ``app.user_id``; ``set_config('role', 'feelings_billing')`` под
    этой ролью суперпользователь сделать может, поэтому здесь проверяется сама граница EXECUTE."""
    couple, params = await _r2_params(sessionmaker, engine)
    for name, sql in R2_BOT_CALLS.items():
        async with engine.connect() as c:
            tx = await c.begin()
            await c.execute(text(f"SET LOCAL ROLE {role}"))
            await c.execute(text("SELECT set_config('app.user_id', '', true)"))
            with pytest.raises(DBAPIError) as ei:
                await c.execute(text(sql), _r2_bind(sql, params))
            assert _sqlstate(ei.value) == "42501", (role, name)
            await tx.rollback()
    await _r2_nothing_paid(engine, couple)


@pytest.mark.parametrize(
    "trick",
    [
        "SET LOCAL ROLE feelings_billing",
        "SET ROLE feelings_billing",
        "SELECT set_config('role', 'feelings_billing', true)",
        "SELECT set_config('role', 'feelings_billing', false)",
        "RESET ROLE; SET LOCAL ROLE feelings_billing",
        'SET LOCAL ROLE "feelings_billing"',
    ],
)
async def test_r2_user_session_cannot_become_billing(
    sessionmaker: SM, engine: AsyncEngine, trick: str
) -> None:
    """Сессия приложения (роль входа ``feelings_api`` → ``feelings_app``) не становится
    ``feelings_billing`` ни ``SET ROLE``, ни ``set_config('role', …)``, ни после ``RESET ROLE``:
    42501 (не член роли), ничего не зачислено."""
    couple, params = await _r2_params(sessionmaker, engine)
    with pytest.raises(DBAPIError) as ei:
        async with user_scope(sessionmaker, None) as s:
            await s.execute(text("SELECT set_config('app.user_id', '', true)"))
            for stmt in trick.split("; "):
                await s.execute(text(stmt))
            sql = R2_BOT_CALLS["credit"]
            await s.execute(text(sql), _r2_bind(sql, params))
    assert _sqlstate(ei.value) == "42501", trick
    await _r2_nothing_paid(engine, couple)


async def test_r2_reset_role_in_user_session_falls_back_to_login_role(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """``RESET ROLE`` в транзакции пользователя возвращает к роли входа ``feelings_api``
    (NOINHERIT, без EXECUTE): все функции бота — 42501; роль входа не может выбрать
    ``feelings_billing`` и после этого."""
    couple, params = await _r2_params(sessionmaker, engine)
    for name, sql in R2_BOT_CALLS.items():
        async with sessionmaker() as session:
            tx = await session.begin()
            await session.execute(text("SET LOCAL ROLE feelings_app"))
            await session.execute(text("RESET ROLE"))
            who = (await session.execute(text("SELECT current_user"))).scalar_one()
            assert who == "feelings_api"
            with pytest.raises(DBAPIError) as ei:
                await session.execute(text(sql), _r2_bind(sql, params))
            assert _sqlstate(ei.value) == "42501", name
            await tx.rollback()
    await _r2_nothing_paid(engine, couple)


async def test_r2_superuser_without_billing_role_is_refused_by_body(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Проверка в теле функции (``current_setting('role')``): даже суперпользователь (EXECUTE
    есть всегда) без ``SET ROLE feelings_billing`` и после ``RESET ROLE`` — 42501
    «billing role only»; с ролью и непустым ``app.user_id`` — 42501, с не-uuid — отказ."""
    couple, params = await _r2_params(sessionmaker, engine)
    for prelude in ((), ("SET LOCAL ROLE feelings_billing", "RESET ROLE")):
        async with engine.connect() as c:
            tx = await c.begin()
            for stmt in prelude:
                await c.execute(text(stmt))
            with pytest.raises(DBAPIError) as ei:
                sql = R2_BOT_CALLS["precheck"]
                await c.execute(text(sql), _r2_bind(sql, params))
            assert _sqlstate(ei.value) == "42501" and "billing role only" in str(ei.value.orig)
            await tx.rollback()
    for value, codes in ((str(uuid.uuid4()), {"42501"}), ("garbage", {"42501", "22P02"})):
        async with engine.connect() as c:
            tx = await c.begin()
            await c.execute(text("SET LOCAL ROLE feelings_billing"))
            await c.execute(text("SELECT set_config('app.user_id', :v, true)"), {"v": value})
            with pytest.raises(DBAPIError) as ei:
                sql = R2_BOT_CALLS["credit"]
                await c.execute(text(sql), _r2_bind(sql, params))
            assert _sqlstate(ei.value) in codes, value
            await tx.rollback()
    await _r2_nothing_paid(engine, couple)


async def test_r2_owner_of_functions_is_refused_by_body(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Владелец функций (в продуктиве — владелец схемы, не суперпользователь) исполняет их
    всегда, без GRANT. Временная роль-владелец в откатываемой транзакции: под ней вызов —
    42501 «billing role only»; роль платежей для неё недоступна без членства."""
    couple, params = await _r2_params(sessionmaker, engine)
    owner = f"a8r2_owner_{uuid.uuid4().hex[:8]}"
    async with engine.connect() as c:
        tx = await c.begin()
        await c.execute(text(f"CREATE ROLE {owner} NOLOGIN NOINHERIT"))
        await c.execute(text(f"GRANT CREATE, USAGE ON SCHEMA public TO {owner}"))
        for sig in BOT_FUNCS:
            await c.execute(text(f"ALTER FUNCTION {sig} OWNER TO {owner}"))
        await c.execute(text(f"SET LOCAL ROLE {owner}"))
        for name, sql in R2_BOT_CALLS.items():
            await c.execute(text("SAVEPOINT s"))
            with pytest.raises(DBAPIError) as ei:
                await c.execute(text(sql), _r2_bind(sql, params))
            assert _sqlstate(ei.value) == "42501", name
            assert "billing role only" in str(ei.value.orig), name
            await c.execute(text("ROLLBACK TO SAVEPOINT s"))
        await tx.rollback()
    await _r2_nothing_paid(engine, couple)


_R2_DEFINER = """
CREATE FUNCTION a8r2_definer_lookup(p varchar) RETURNS bigint
    LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$ SELECT count(*) FROM public.app_bot_payment_lookup(p) $f$
"""
_R2_INVOKER_SET_ROLE = """
CREATE FUNCTION a8r2_invoker_lookup(p varchar) RETURNS bigint
    LANGUAGE sql SET role = 'feelings_billing' SET search_path = public, pg_temp
    AS $f$ SELECT count(*) FROM public.app_bot_payment_lookup(p) $f$
"""
_R2_DEFINER_SET_ROLE = """
CREATE FUNCTION a8r2_definer_role_lookup(p varchar) RETURNS bigint
    LANGUAGE sql SECURITY DEFINER SET role = 'feelings_billing' SET search_path = public, pg_temp
    AS $f$ SELECT count(*) FROM public.app_bot_payment_lookup(p) $f$
"""
WRAPPERS = (
    "a8r2_definer_lookup(varchar)",
    "a8r2_invoker_lookup(varchar)",
    "a8r2_definer_role_lookup(varchar)",
)


async def test_r2_wrapper_functions_do_not_open_billing(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Вызов функции бота из другой функции:

    - SECURITY DEFINER суперпользователя (EXECUTE на функцию бота у владельца есть), вызванная
      сессией пользователя, — 42501 «billing role only»: SECURITY DEFINER не меняет ``role``;
    - SECURITY DEFINER с ``SET role = feelings_billing`` — PostgreSQL не даёт менять ``role``
      внутри SECURITY DEFINER (42501 при вызове);
    - функция вызывающего с ``SET role = feelings_billing`` — при вызове сессией пользователя
      42501 (смена роли проверяется по членству роли сеанса).

    Обёртки — во временных функциях тестовой БД, удаляются в ``finally``."""
    couple, _params = await _r2_params(sessionmaker, engine)
    a = await _su(
        engine, "SELECT cm.user_id FROM couple_members cm WHERE cm.couple_id = :c LIMIT 1", c=couple
    )
    try:
        async with engine.begin() as c:
            for ddl in (_R2_DEFINER, _R2_INVOKER_SET_ROLE, _R2_DEFINER_SET_ROLE):
                await c.execute(text(ddl))
            for f in WRAPPERS:
                await c.execute(text(f"REVOKE ALL ON FUNCTION {f} FROM PUBLIC"))
                await c.execute(text(f"GRANT EXECUTE ON FUNCTION {f} TO feelings_app"))
        for f in (w.split("(")[0] for w in WRAPPERS):
            for uid in (a, None):
                with pytest.raises(DBAPIError) as ei:
                    async with user_scope(sessionmaker, uid) as s:
                        if uid is None:
                            await s.execute(text("SELECT set_config('app.user_id', '', true)"))
                        await s.execute(text(f"SELECT {f}(:ch)"), {"ch": _charge()})
                assert _sqlstate(ei.value) == "42501", (f, uid)
    finally:
        async with engine.begin() as c:
            for f in WRAPPERS:
                await c.execute(text(f"DROP FUNCTION IF EXISTS {f}"))
    await _r2_nothing_paid(engine, couple)


async def test_r2_catalog_billing_rights_are_exact(engine: AsyncEngine) -> None:
    """Каталог на голове: EXECUTE на функции бота — только у ``feelings_billing`` (и владельца),
    ни у PUBLIC, ни у ролей приложения; у ``feelings_billing`` нет EXECUTE ни на одну функцию
    пользователя и служебную; ни одна функция схемы не задаёт ``role`` в своих настройках;
    в ``feelings_billing`` никто из ролей приложения не входит, сама она ни в одну не входит."""
    acl = await _su(
        engine,
        "SELECT string_agg(p.oid::regprocedure::text || '=' || "
        "coalesce((SELECT string_agg(x.grantee::regrole::text, '+' ORDER BY x.grantee::regrole::text) "
        "FROM aclexplode(p.proacl) x WHERE x.privilege_type = 'EXECUTE' "
        "AND x.grantee <> p.proowner AND x.grantee <> 0), 'PUBLIC?'), ',' ORDER BY p.proname) "
        "FROM pg_proc p WHERE p.proname LIKE 'app\\_bot\\_payment\\_%'",
    )
    assert acl.count("=feelings_billing") == 4 and "feelings_app" not in acl, acl
    public = await _su(
        engine,
        "SELECT count(*) FROM pg_proc p, aclexplode(p.proacl) x WHERE p.proname LIKE "
        "'app\\_bot\\_payment\\_%' AND x.grantee = 0",
    )
    assert public == 0
    for f in USER_FUNCS + INTERNAL_FUNCS:
        assert not await _su(
            engine, "SELECT has_function_privilege('feelings_billing', :f, 'EXECUTE')", f=f
        ), f
    role_cfg = await _su(
        engine,
        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND EXISTS (SELECT 1 FROM unnest(p.proconfig) c "
        "WHERE c LIKE 'role=%' OR c LIKE 'session_authorization=%')",
    )
    assert role_cfg == 0
    for r in R2_ROLES:
        assert not await _su(engine, "SELECT pg_has_role(:r, 'feelings_billing', 'MEMBER')", r=r), r
        assert not await _su(engine, "SELECT pg_has_role('feelings_billing', :r, 'MEMBER')", r=r), r


# --------------------------------------------------------------------------- 0019 в транзакции


def _r2_alembic(sync_conn: Any, *args: str) -> None:
    """Команда Alembic на **этом** соединении, внутри уже открытой транзакции (env.py берёт
    ``config.attributes['connection']``; при внешней транзакции Alembic своей не открывает) —
    всё откатывается вместе с транзакцией теста (роли кластера не меняются, CLAUDE.md)."""
    import argparse

    from alembic import command
    from alembic.config import Config

    # Без файла ini: env.py тогда не вызывает fileConfig — журналы процесса тестов (caplog других
    # тестов) не перенастраиваются и не отключаются.
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.cmd_opts = argparse.Namespace(x=[f"db_url={TEST_DATABASE_URL}"])
    cfg.attributes["connection"] = sync_conn
    getattr(command, args[0])(cfg, *args[1:])


async def _r2_state(c: Any) -> tuple[str, bool, bool, str]:
    """(версия, EXECUTE у feelings_app на все функции бота, у feelings_billing, атрибуты роли)."""
    # По ACL функции, а не has_function_privilege: у роли с SUPERUSER он всегда true.
    q = (
        "SELECT EXISTS (SELECT 1 FROM pg_proc p, aclexplode(p.proacl) x "
        "WHERE p.oid = CAST(:f AS regprocedure) AND x.grantee = CAST(to_regrole(:r) AS oid) "
        "AND x.privilege_type = 'EXECUTE')"
    )
    version = (await c.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    app = all(
        [(await c.execute(text(q), {"r": "feelings_app", "f": f})).scalar_one() for f in BOT_FUNCS]
    )
    bill = all(
        [
            (await c.execute(text(q), {"r": "feelings_billing", "f": f})).scalar_one()
            for f in BOT_FUNCS
        ]
    )
    attrs = (
        await c.execute(
            text(
                "SELECT concat_ws(',', rolcanlogin, rolinherit, rolsuper, rolcreaterole, "
                "rolcreatedb, rolreplication, rolbypassrls) FROM pg_roles "
                "WHERE rolname = 'feelings_billing'"
            )
        )
    ).scalar_one()
    return str(version), bool(app), bool(bill), str(attrs)


async def test_r2_rerun_0019_with_existing_role_in_transaction(engine: AsyncEngine) -> None:
    """Роль уже есть (общая для кластера, после отката 0019 не удаляется): повторный upgrade
    0019 проходит, роль не пересоздаётся и не меняется, права выдаются заново; дважды подряд
    downgrade/upgrade — то же. Всё в одной откатываемой транзакции."""
    async with engine.connect() as c:
        tx = await c.begin()
        oid = (await c.execute(text("SELECT to_regrole('feelings_billing')::oid"))).scalar_one()
        assert await _r2_state(c) == (HEAD, False, True, "f,f,f,f,f,f,f")
        for _ in range(2):
            await c.run_sync(_r2_alembic, "downgrade", "0018")
            assert await _r2_state(c) == ("0018", True, False, "f,f,f,f,f,f,f")
            await c.run_sync(_r2_alembic, "upgrade", "head")
            assert await _r2_state(c) == (HEAD, False, True, "f,f,f,f,f,f,f")
        again = (await c.execute(text("SELECT to_regrole('feelings_billing')::oid"))).scalar_one()
        assert again == oid
        await tx.rollback()
    async with engine.connect() as c:
        assert await _r2_state(c) == (HEAD, False, True, "f,f,f,f,f,f,f")


@pytest.mark.parametrize("attr", ROLE_ATTRS)
async def test_r2_existing_role_with_dangerous_attribute_stops_0019(
    engine: AsyncEngine, attr: str
) -> None:
    """Роль ``feelings_billing`` уже есть и имеет LOGIN / INHERIT / особые права: upgrade 0019
    останавливается с понятной ошибкой и ничего не меняет — версия 0018, EXECUTE функций бота у
    ``feelings_app`` как в 0018, у роли платежей прав нет, атрибут роли не снят. Откат."""
    async with engine.connect() as c:
        tx = await c.begin()
        await c.run_sync(_r2_alembic, "downgrade", "0018")
        await c.execute(text(f"ALTER ROLE feelings_billing {attr}"))
        await c.execute(text("SAVEPOINT before_upgrade"))
        with pytest.raises(DBAPIError) as ei:
            await c.run_sync(_r2_alembic, "upgrade", "head")
        assert "роль feelings_billing уже есть" in str(ei.value.orig), attr
        await c.execute(text("ROLLBACK TO SAVEPOINT before_upgrade"))
        version, app, bill, attrs = await _r2_state(c)
        assert (version, app, bill) == ("0018", True, False)
        assert attrs.split(",")[ROLE_ATTRS.index(attr)] == "t"
        await tx.rollback()
    async with engine.connect() as c:
        assert await _r2_state(c) == (HEAD, False, True, "f,f,f,f,f,f,f")


@pytest.mark.parametrize("member", ["feelings_api", "feelings_app"])
async def test_r2_existing_role_with_app_member_stops_0019(
    sessionmaker: SM, engine: AsyncEngine, member: str
) -> None:
    """АР-50 п.7: «feelings_api не член feelings_billing» — на этом держится граница. Роль уже
    есть, и в неё входит роль приложения: ожидается, что upgrade 0019 остановится, как при
    LOGIN/INHERIT. Всё в откатываемой транзакции."""
    async with engine.connect() as c:
        tx = await c.begin()
        await c.run_sync(_r2_alembic, "downgrade", "0018")
        await c.execute(text(f"GRANT feelings_billing TO {member}"))
        await c.execute(text("SAVEPOINT before_upgrade"))
        stopped = False
        try:
            await c.run_sync(_r2_alembic, "upgrade", "head")
        except DBAPIError:
            stopped = True
            await c.execute(text("ROLLBACK TO SAVEPOINT before_upgrade"))
        await tx.rollback()
    assert stopped, member


async def test_r2_role_membership_hole_is_real(engine: AsyncEngine) -> None:
    """К A8-4.4-D3 — что даёт членство: в откатываемой транзакции ``GRANT feelings_billing TO
    feelings_api`` даёт роли входа API право ``SET`` на роль платежей (``pg_has_role … 'SET'``),
    то есть ``SET LOCAL ROLE feelings_billing`` в сессии пользователя. Сам вызов сессией API здесь
    не проверить: членство увидела бы только другая сессия после фиксации, а роли кластера
    меняются только в откатываемых транзакциях (CLAUDE.md). После отката членства нет."""
    async with engine.connect() as c:
        tx = await c.begin()
        await c.execute(text("GRANT feelings_billing TO feelings_api WITH INHERIT FALSE, SET TRUE"))
        assert await c.scalar(text("SELECT pg_has_role('feelings_api', 'feelings_billing', 'SET')"))
        await tx.rollback()
    async with engine.connect() as c:
        assert not await c.scalar(
            text("SELECT pg_has_role('feelings_api', 'feelings_billing', 'MEMBER')")
        )


# --------------------------------------------------------------------------- откат с данными


async def test_r2_downgrade_upgrade_switches_period_rule_and_keeps_dates(
    sessionmaker: SM, engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """head → 0018 → head с разрывом после возврата: под 0017-правилом (после отката) новая оплата
    в разрыве встаёт после будущего периода, доступа нет, бот снова — ``feelings_app`` в служебном
    контексте; после возврата к 0019 даты этого периода не меняются, разрыв остаётся, а следующая
    оплата начинается сейчас и даёт доступ; ``feelings_app`` снова без EXECUTE; ``alembic check``."""
    a, b, _couple_id = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    tg = await _tg_id(engine, a)
    ch1 = _charge()
    p1 = await _credit(sessionmaker, engine, inv, a, ch1)
    p2 = await _credit(sessionmaker, engine, inv, b, _charge())
    await _one(sessionmaker, None, REFUNDED, ch=ch1)
    old_rule: dict[str, Any] = {}
    try:
        alembic("downgrade", "0018")
        await engine.dispose()
        await app_engine.dispose()
        async with user_scope(sessionmaker, None) as s:
            row = (
                (
                    await s.execute(
                        text(CREDIT),
                        {"i": inv, "tg": tg, "amount": 250, "cur": "XTR", "ch": _charge()},
                    )
                )
                .mappings()
                .one()
            )
            old_rule = dict(row)
        assert old_rule["result"] == "credited"
        assert old_rule["period_end"] == p2["period_end"] + timedelta(days=30)
        assert await _call(sessionmaker, a, HAS_ACCESS) is False
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    assert await _su(
        engine, "SELECT period_end FROM subscriptions WHERE id = :s", s=old_rule["subscription_id"]
    ) == old_rule["period_end"]  # fmt: skip
    assert await _call(sessionmaker, a, HAS_ACCESS) is False
    st = await _one(sessionmaker, b, STATUS)
    assert (st["status"], st["period_end"]) == ("canceled", p1["period_end"])
    before = datetime.now(UTC)
    p4 = await _credit(sessionmaker, engine, inv, b, _charge())
    # 0020 (ревью 4.4 р.2 MAJOR-2; правка A1 — на проверку A8): P2 и период «старого правила»
    # (60 дней подряд) начинаются внутри нового периода — его конец продлевается на них, 30
    # оплаченных дней не теряются: доступ до now() + 90 дней (было: перекрытие терялось).
    span = timedelta(days=90)
    assert before + span <= p4["period_end"] <= datetime.now(UTC) + span
    for who in (a, b):
        assert await _call(sessionmaker, who, HAS_ACCESS) is True
    st = await _one(sessionmaker, a, STATUS)
    assert (st["status"], st["period_end"]) == ("active", p4["period_end"])
    with pytest.raises(DBAPIError) as ei:
        async with user_scope(sessionmaker, None) as s:
            await s.execute(text(LOOKUP), {"ch": ch1})
    assert _sqlstate(ei.value) == "42501"
    alembic("check")


# --------------------------------------------------------------------------- разрыв после возврата


async def test_r2_refund_middle_period_new_payment_fills_gap(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """P1 [now, +30], P2 [+30, +60], P3 [+60, +90]; возврат P2: доступ до конца P1, статус —
    ``active`` до конца P1 (цепочка обрывается на разрыве); новая оплата встаёт в конец
    непрерывного доступа — ровно в разрыв [+30, +60] — и цепочка снова доходит до конца P3."""
    a, b, _c = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    p1 = await _credit(sessionmaker, engine, inv, a, _charge())
    ch2 = _charge()
    p2 = await _credit(sessionmaker, engine, inv, a, ch2)
    p3 = await _credit(sessionmaker, engine, inv, b, _charge())
    assert p3["period_end"] == p1["period_end"] + timedelta(days=60)
    await _one(sessionmaker, None, REFUNDED, ch=ch2)
    st = await _one(sessionmaker, b, STATUS)
    assert (st["status"], st["period_end"], st["has_access"]) == ("active", p1["period_end"], True)
    p4 = await _credit(sessionmaker, engine, inv, b, _charge())
    assert p4["period_end"] == p2["period_end"]
    st = await _one(sessionmaker, a, STATUS)
    assert (st["status"], st["period_end"]) == ("active", p3["period_end"])


async def test_r2_refund_all_but_future_then_two_payments(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """P1, P2, P3 подряд; возврат P1 и P2 — разрыв до начала P3: доступа нет, статус по
    последнему начавшемуся — ``canceled`` с концом P1 (не «истёк» с будущей датой); оплата —
    сразу [now, +30], затем вторая — с конца P4 через P3 (0020: продлена на длину P3)."""
    a, b, _c = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    ch1, ch2 = _charge(), _charge()
    p1 = await _credit(sessionmaker, engine, inv, a, ch1)
    p2 = await _credit(sessionmaker, engine, inv, a, ch2)
    p3 = await _credit(sessionmaker, engine, inv, a, _charge())
    for ch in (ch1, ch2):
        await _one(sessionmaker, None, REFUNDED, ch=ch)
    st = await _one(sessionmaker, b, STATUS)
    assert (st["status"], st["period_end"], st["has_access"]) == (
        "canceled",
        p1["period_end"],
        False,
    )
    p4 = await _credit(sessionmaker, engine, inv, b, _charge())
    assert abs(p4["period_end"] - p1["period_end"]) < timedelta(seconds=30)
    assert (await _one(sessionmaker, a, STATUS))["period_end"] == p4["period_end"]
    p5 = await _credit(sessionmaker, engine, inv, a, _charge())
    # 0020 (ревью 4.4 р.2 MAJOR-2; правка A1 — на проверку A8): P5 встаёт после P4, P3 начинается
    # внутри P5 (на время, прошедшее между оплатами P1 и P4), — конец P5 продлевается на длину P3,
    # ни секунды оплаченного не теряется (было: P5 = P4 + 30 дней, перекрытие с P3 терялось).
    assert p5["period_end"] == p3["period_end"] + (p4["period_end"] - p1["period_end"])
    st = await _one(sessionmaker, a, STATUS)
    assert (st["status"], st["period_end"]) == ("active", p5["period_end"])
    assert p5["period_end"] > p2["period_end"] - timedelta(seconds=30)


# --------------------------------------------------------------------------- статус после распада


async def test_r2_status_after_dissolution_for_both_and_new_couple(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Ревью MINOR-3 (0019): оплаченный период распавшейся пары дослуживает (§11 п.6) — у обоих
    бывших партнёров статус ``active``, тариф и конец цепочки той пары; ``paid_by_me`` — у
    плательщика; ``couple_*`` — текущей пары (нет — NULL). Новая ожидающая пара — статус всё
    ещё ``active`` по старой, ``couple_status = pending``; новая активная пара с пробным — статус
    ``trial`` текущей пары, доступ есть."""
    a, b, old = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    await _one(sessionmaker, a, TRIAL, d=7)
    await _credit(sessionmaker, engine, inv, a, _charge())
    p2 = await _credit(sessionmaker, engine, inv, a, _charge())
    yinv = await _invoice(sessionmaker, b, plan="year", amount=2000, days=365)
    py = await _credit(sessionmaker, engine, yinv, b, _charge(), amount=2000)
    assert py["period_end"] == p2["period_end"] + timedelta(days=365)
    await _call(sessionmaker, a, LEAVE)
    for who, mine in ((a, False), (b, True)):
        st = await _one(sessionmaker, who, STATUS)
        assert (st["status"], st["plan"], st["period_end"], st["has_access"]) == (
            "active", "year", py["period_end"], True,
        ), who  # fmt: skip
        assert st["couple_id"] is None and st["couple_status"] is None
        assert st["paid_by_me"] is mine and st["trial_used"] is False
    new = uuid.UUID(str(await _call(sessionmaker, a, "SELECT app_create_couple()")))
    st = await _one(sessionmaker, a, STATUS)
    assert (st["status"], st["plan"], st["couple_id"], st["couple_status"]) == (
        "active", "year", new, "pending",
    )  # fmt: skip
    token = uuid.uuid4().bytes + uuid.uuid4().bytes
    await _call(sessionmaker, a, "SELECT app_issue_invite(:h)", h=token)
    c = await _user_r2(engine)
    await _call(sessionmaker, c, ACCEPT, h=token)
    await _one(sessionmaker, c, TRIAL, d=7)
    st = await _one(sessionmaker, a, STATUS)
    assert (st["status"], st["couple_id"], st["has_access"]) == ("trial", new, True)
    assert st["trial_used"] is True
    # Новый партнёр к оплате старой пары отношения не имеет: после конца пробного у него — без
    # доступа (пробный здесь ещё идёт — доступ есть, но статус — пробный текущей пары).
    st_c = await _one(sessionmaker, c, STATUS)
    assert (st_c["status"], st_c["couple_id"]) == ("trial", new)
    assert await _subs_count(engine, old) == 4


async def _user_r2(engine: AsyncEngine) -> uuid.UUID:
    from tests.test_couple_functions import _user

    return await _user(engine, "Кира")


async def test_r2_status_after_dissolution_trial_only_and_refunded(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Распад пары только с пробным — статус ``none`` (пары нет), доступа нет; распад пары, чей
    единственный оплаченный период возвращён, — тоже ``none`` без доступа; партнёр, оставшийся
    в распавшейся паре, видит то же."""
    a, b, _c = await _couple(sessionmaker, engine)
    await _one(sessionmaker, a, TRIAL, d=7)
    await _call(sessionmaker, b, LEAVE)
    for who in (a, b):
        st = await _one(sessionmaker, who, STATUS)
        assert (st["status"], st["has_access"], st["couple_id"]) == ("none", False, None), who
    x, y, _c2 = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, x)
    ch = _charge()
    await _credit(sessionmaker, engine, inv, x, ch)
    await _one(sessionmaker, None, REFUNDED, ch=ch)
    await _call(sessionmaker, x, LEAVE)
    for who in (x, y):
        st = await _one(sessionmaker, who, STATUS)
        assert (st["status"], st["has_access"]) == ("none", False), who


# --------------------------------------------------------------------------- предел счетов залпом


async def test_r2_invoice_limit_parallel_burst_both_partners(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Ревью MINOR-5 (0019): 30 одновременных запросов счёта обоими партнёрами с разными условиями
    без удержания пары — выпущено ровно 20, остальные 10 — FC002; ни одного 40P01/40001/23505.
    Затем 10 одновременных запросов с одинаковыми условиями при 19 действующих — один новый счёт
    на всех, остальные ``reused`` с тем же id."""
    a, b, couple = await _couple(sessionmaker, engine)
    res = await asyncio.gather(
        *(
            _one(sessionmaker, (a, b)[n % 2], INVOICE, **{**MONTH, "amount": 300 + n})
            for n in range(30)
        ),
        return_exceptions=True,
    )
    codes = _codes(list(res))
    assert sorted(str(c) for c in codes) == ["FC002"] * 10, codes
    assert sum(isinstance(r, dict) for r in res) == 20
    open_now = "SELECT count(*) FROM payments WHERE couple_id = :c AND expires_at > now()"
    assert await _su(engine, open_now, c=couple) == 20
    await _su(
        engine,
        "UPDATE payments SET expires_at = now() - interval '1 minute' WHERE couple_id = :c "
        "AND amount = 300",
        c=couple,
    )
    same = await asyncio.gather(
        *(
            _one(sessionmaker, (a, b)[n % 2], INVOICE, **{**MONTH, "amount": 999})
            for n in range(10)
        ),
        return_exceptions=True,
    )
    assert _codes(list(same)) == []
    got = [r for r in same if isinstance(r, dict)]
    assert sorted(r["reused"] for r in got) == [False] + [True] * 9
    assert len({r["invoice_id"] for r in got}) == 1
    assert await _su(engine, open_now, c=couple) == 20


# =========================================================================== раунд 3 (0020)
#
# Ревью 4.4 э.1 р.2 MAJOR-1 / A8-4.4-D3 (страж ``migrations/billing_guard.py``), MAJOR-2 (оплаченные
# дни после возврата). Всё — в откатываемых транзакциях суперпользователя: роли кластера и данные
# не меняются (CLAUDE.md). Внутри одной транзакции ``now()`` постоянен — периоды задаются точно
# относительно него, а зачисление (под ``SET LOCAL ROLE feelings_billing``) видит тот же ``now()``.

GUARD_R3 = migration_module("billing_guard.py")
DAY = 86400


async def _r3_now(c: Any) -> datetime:
    return (await c.execute(text("SELECT now()"))).scalar_one()  # type: ignore[no-any-return]


async def _r3_period(
    c: Any, couple: uuid.UUID, payer: uuid.UUID, start: int, end: int, status: str = "active"
) -> None:
    """Период пары [now()+start, now()+end] (секунды) — в транзакции теста."""
    plan = "trial" if status == "trial" else "month"
    await c.execute(
        text(
            "INSERT INTO subscriptions (couple_id, payer_id, plan, status, period_start, period_end) "
            "VALUES (:c, :p, :plan, CAST(:st AS subscription_status), "
            "now() + make_interval(secs => :s), now() + make_interval(secs => :e))"
        ),
        {"c": couple, "p": payer, "plan": plan, "st": status, "s": start, "e": end},
    )


async def _r3_credit(c: Any, inv: uuid.UUID, tg: int) -> dict[str, Any]:
    """Зачисление в транзакции теста так, как его делает бот (роль платежей, пустой user_id)."""
    await c.execute(text("SET LOCAL ROLE feelings_billing"))
    await c.execute(text("SELECT set_config('app.user_id', '', true)"))
    row = (
        (
            await c.execute(
                text(CREDIT), {"i": inv, "tg": tg, "amount": 250, "cur": "XTR", "ch": _charge()}
            )
        )
        .mappings()
        .one()
    )
    await c.execute(text("RESET ROLE"))
    return dict(row)


def _r3_uncovered(start: float, end: float, others: list[tuple[float, float]]) -> float:
    """Сколько секунд из [start, end] не покрыто другими периодами (их объединением)."""
    covered = 0.0
    cur = start
    for s, e in sorted(others):
        s, e = max(s, cur), min(e, end)
        if e > s:
            covered += e - s
            cur = e
    return (end - start) - covered


# (периоды пары до оплаты: (начало, конец, статус) в секундах от now(); ожидаемые начало и конец
# нового 30-дневного периода).
R3_BOUNDARY = {
    "touching_natural_end": ([(30 * DAY, 60 * DAY, "active")], 0, 30 * DAY),
    "one_second_overlap": ([(30 * DAY - 1, 60 * DAY, "active")], 0, 60 * DAY + 1),
    "future_starts_1s_after_now": ([(1, 30 * DAY, "active")], 0, 60 * DAY - 1),
    "two_inside_and_third_in_extension": (
        [
            (5 * DAY, 10 * DAY, "active"),
            (20 * DAY, 25 * DAY, "active"),
            (38 * DAY, 50 * DAY, "active"),
        ],
        0,
        52 * DAY,
    ),
    "overlapping_futures": (
        [(5 * DAY, 15 * DAY, "active"), (10 * DAY, 20 * DAY, "active")],
        0,
        45 * DAY,
    ),
    "canceled_future_ignored": ([(5 * DAY, 10 * DAY, "canceled")], 0, 30 * DAY),
    "long_future": ([(29 * DAY, 400 * DAY, "active")], 0, 401 * DAY),
    "review_example_refunded_current": (
        [(-10 * DAY, 20 * DAY, "canceled"), (20 * DAY, 50 * DAY, "active")],
        0,
        60 * DAY,
    ),
    "gap_after_current_then_future": (
        [(-DAY, 10 * DAY, "active"), (20 * DAY, 40 * DAY, "active")],
        10 * DAY,
        60 * DAY,
    ),
    "contiguous_chain_no_gap": (
        [(-DAY, 10 * DAY, "active"), (10 * DAY, 20 * DAY, "active")],
        20 * DAY,
        50 * DAY,
    ),
    "future_trial_inside": ([(5 * DAY, 12 * DAY, "trial")], 0, 37 * DAY),
    "refund_everything_future_gone": (
        [(-5 * DAY, 25 * DAY, "canceled"), (25 * DAY, 55 * DAY, "canceled")],
        0,
        30 * DAY,
    ),
}


@pytest.mark.parametrize("case", list(R3_BOUNDARY))
async def test_r3_credit_boundaries_keep_paid_days_exact(
    sessionmaker: SM, engine: AsyncEngine, case: str
) -> None:
    """АР-50 п.4, ревью р.2 MAJOR-2 (0020), ТЗ §11 п.1: граничные разрывы после возврата — новый
    период начинается в конце непрерывного доступа от ``now()`` и длится, пока не наберёт ровно 30
    дней, не покрытых другими (``trial``/``active``) периодами; касание без перекрытия — без
    продления; ``canceled`` не учитываются. Точность — до микросекунды (один ``now()``)."""
    periods, exp_start, exp_end = R3_BOUNDARY[case]
    a, _b, couple = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    tg = await _tg_id(engine, a)
    async with engine.connect() as c:
        tx = await c.begin()
        now = await _r3_now(c)
        for s, e, st in periods:
            await _r3_period(c, couple, a, s, e, st)
        row = await _r3_credit(c, inv, tg)
        assert row["result"] == "credited", case
        start, end = (
            await c.execute(
                text("SELECT period_start, period_end FROM subscriptions WHERE id = :s"),
                {"s": row["subscription_id"]},
            )
        ).one()
        assert start == now + timedelta(seconds=exp_start), case
        assert end == row["period_end"] == now + timedelta(seconds=exp_end), case
        live = [(float(s), float(e)) for s, e, st in periods if st != "canceled"]
        paid = _r3_uncovered(float(exp_start), float(exp_end), live)
        assert paid == 30 * DAY, (case, paid)
        await tx.rollback()
    assert await _subs_count(engine, couple) == 0


async def test_r3_upgrade_downgrade_upgrade_switches_credit_rule(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """head (0020) → 0019 → head → 0019 → head в одной откатываемой транзакции, пример ревью
    (P1 [-10 д, +20 д] возвращён, P2 [+20, +50]): под 0020 оплата — [now, +60], под 0019 —
    [now, +30] (правило 0019), после повторного upgrade снова [now, +60]; страж исполняется на
    каждом шаге, версия в конце — голова, функции бота — только у ``feelings_billing``."""
    a, _b, couple = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    tg = await _tg_id(engine, a)
    ends: list[tuple[str, timedelta]] = []
    async with engine.connect() as c:
        tx = await c.begin()
        now = await _r3_now(c)
        await _r3_period(c, couple, a, -10 * DAY, 20 * DAY, "canceled")
        await _r3_period(c, couple, a, 20 * DAY, 50 * DAY, "active")
        for step in ("head", "0019", "head", "0019", "head"):
            if step == "0019":
                await c.run_sync(alembic_on, "downgrade", "0019")
            elif ends:
                await c.run_sync(alembic_on, "upgrade", "head")
            version = (await c.execute(text("SELECT version_num FROM alembic_version"))).scalar()
            await c.execute(text("SAVEPOINT pay"))
            row = await _r3_credit(c, inv, tg)
            ends.append((str(version), row["period_end"] - now))
            await c.execute(text("ROLLBACK TO SAVEPOINT pay"))
        assert ends == [
            (HEAD, timedelta(days=60)),
            ("0019", timedelta(days=30)),
            (HEAD, timedelta(days=60)),
            ("0019", timedelta(days=30)),
            (HEAD, timedelta(days=60)),
        ]
        assert await _r2_state(c) == (HEAD, False, True, "f,f,f,f,f,f,f")
        await c.execute(text(GUARD_R3.GUARD_SQL))
        await tx.rollback()
    assert await _subs_count(engine, couple) == 0


# --------------------------------------------------------------------------- страж: обходы

# Подготовка в откатываемой транзакции; ожидаемая остановка — часть сообщения стража.
R3_GUARD_STOPS = {
    "api_admin_only_on_bot_login": (
        [
            "CREATE ROLE feelings_billing_login LOGIN NOINHERIT",
            "GRANT feelings_billing TO feelings_billing_login WITH INHERIT FALSE, SET TRUE",
            "GRANT feelings_billing_login TO feelings_api WITH ADMIN TRUE, INHERIT FALSE, SET FALSE",
        ],
        "роли приложения: feelings_api",
    ),
    "api_set_on_bot_login": (
        [
            "CREATE ROLE feelings_billing_login LOGIN NOINHERIT",
            "GRANT feelings_billing TO feelings_billing_login WITH INHERIT FALSE, SET TRUE",
            "GRANT feelings_billing_login TO feelings_api WITH INHERIT FALSE, SET TRUE",
        ],
        "роли приложения: feelings_api",
    ),
    "stranger_set_on_bot_login": (
        [
            "CREATE ROLE feelings_billing_login LOGIN NOINHERIT",
            "GRANT feelings_billing TO feelings_billing_login WITH INHERIT FALSE, SET TRUE",
            "CREATE ROLE a8r3_y LOGIN NOINHERIT",
            "GRANT feelings_billing_login TO a8r3_y WITH INHERIT FALSE, SET TRUE",
        ],
        "не только роль входа бота: a8r3_y",
    ),
    "worker_usage_three_levels": (
        [
            "CREATE ROLE a8r3_l1 NOLOGIN",
            "CREATE ROLE a8r3_l2 NOLOGIN",
            "GRANT feelings_billing TO a8r3_l1 WITH INHERIT TRUE, SET FALSE",
            "GRANT a8r3_l1 TO a8r3_l2 WITH INHERIT TRUE, SET FALSE",
            "GRANT a8r3_l2 TO feelings_worker WITH INHERIT TRUE, SET FALSE",
        ],
        "роли приложения: feelings_worker",
    ),
    "stranger_set_via_set_only_chain": (
        [
            "CREATE ROLE a8r3_l1 NOLOGIN NOINHERIT",
            "GRANT feelings_billing TO a8r3_l1 WITH INHERIT FALSE, SET TRUE",
            "CREATE ROLE a8r3_z LOGIN NOINHERIT",
            "GRANT a8r3_l1 TO a8r3_z WITH INHERIT FALSE, SET TRUE",
        ],
        "не только роль входа бота: a8r3_l1, a8r3_z",
    ),
    "bot_login_in_predefined": (
        [
            "CREATE ROLE feelings_billing_login LOGIN NOINHERIT",
            "GRANT feelings_billing TO feelings_billing_login WITH INHERIT FALSE, SET TRUE",
            "GRANT pg_read_all_data TO feelings_billing_login",
        ],
        "входит в другие роли: pg_read_all_data",
    ),
    "billing_in_predefined": (
        ["GRANT pg_write_all_data TO feelings_billing"],
        "член других ролей: pg_write_all_data",
    ),
    "bot_login_superuser": (
        ["CREATE ROLE feelings_billing_login LOGIN SUPERUSER"],
        "роль входа бота feelings_billing_login входит в другие роли",
    ),
    "renamed_stranger_as_bot_login_in_app": (
        [
            "CREATE ROLE a8r3_r LOGIN NOINHERIT",
            "GRANT feelings_app TO a8r3_r WITH INHERIT FALSE, SET TRUE",
            "GRANT feelings_billing TO a8r3_r WITH INHERIT FALSE, SET TRUE",
            "ALTER ROLE a8r3_r RENAME TO feelings_billing_login",
        ],
        "входит в другие роли: feelings_app",
    ),
}


@pytest.mark.parametrize("case", list(R3_GUARD_STOPS))
async def test_r3_guard_stops_bypass_attempts(engine: AsyncEngine, case: str) -> None:
    """АР-50 п.7, ревью р.2 MAJOR-1 (0020): попытки обойти страж — ADMIN/SET у ролей приложения
    через роль входа бота, посторонний с SET на роль входа бота, USAGE через три уровня,
    цепочка только из SET, встроенные роли, суперпользователь и переименование под имя роли
    входа бота — ``alembic upgrade head`` (страж из env.py) останавливается. Откат."""
    setup, stop = R3_GUARD_STOPS[case]
    async with engine.connect() as c:
        tx = await c.begin()
        for sql in setup:
            await c.execute(text(sql))
        await c.execute(text("SAVEPOINT g"))
        with pytest.raises(DBAPIError) as ei:
            await c.run_sync(alembic_on, "upgrade", "head")
        assert stop in str(ei.value.orig), (case, str(ei.value.orig))
        await c.execute(text("ROLLBACK TO SAVEPOINT g"))
        await tx.rollback()
    await _r3_no_leftovers(engine)


# ADMIN у роли, которая не является владельцем схемы: страж пропускает, а роль затем выдаёт себе
# (или роли API) SET на ``feelings_billing``. (подготовка, эскалация от имени роли, кто получает SET)
R3_ADMIN_HOLES = {
    "other_login_admin_only": (
        [
            "CREATE ROLE a8r3_wlogin LOGIN NOINHERIT",
            "GRANT feelings_billing TO a8r3_wlogin WITH ADMIN TRUE, INHERIT FALSE, SET FALSE",
        ],
        ("a8r3_wlogin", "GRANT feelings_billing TO a8r3_wlogin WITH INHERIT FALSE, SET TRUE"),
        "a8r3_wlogin",
    ),
    "login_inherits_admin_of_group": (
        [
            "CREATE ROLE a8r3_grp NOLOGIN NOINHERIT",
            "GRANT feelings_billing TO a8r3_grp WITH ADMIN TRUE, INHERIT FALSE, SET FALSE",
            "CREATE ROLE a8r3_login LOGIN INHERIT",
            "GRANT a8r3_grp TO a8r3_login WITH INHERIT TRUE, SET FALSE",
        ],
        (
            "a8r3_login",
            "GRANT feelings_billing TO a8r3_login WITH INHERIT FALSE, SET TRUE GRANTED BY a8r3_grp",
        ),
        "a8r3_login",
    ),
    "bot_login_with_admin_grants_api": (
        [
            "CREATE ROLE feelings_billing_login LOGIN NOINHERIT",
            "GRANT feelings_billing TO feelings_billing_login WITH ADMIN TRUE, INHERIT FALSE, "
            "SET TRUE",
        ],
        (
            "feelings_billing_login",
            "GRANT feelings_billing TO feelings_api WITH INHERIT FALSE, SET TRUE",
        ),
        "feelings_api",
    ),
    "stranger_admin_on_bot_login": (
        [
            "CREATE ROLE feelings_billing_login LOGIN NOINHERIT",
            "GRANT feelings_billing TO feelings_billing_login WITH INHERIT FALSE, SET TRUE",
            "CREATE ROLE a8r3_x LOGIN NOINHERIT",
            "GRANT feelings_billing_login TO a8r3_x WITH ADMIN TRUE, INHERIT FALSE, SET FALSE",
        ],
        ("a8r3_x", "GRANT feelings_billing_login TO a8r3_x WITH INHERIT FALSE, SET TRUE"),
        "a8r3_x",
    ),
}


async def _r3_no_leftovers(engine: AsyncEngine) -> None:
    async with engine.connect() as c:
        left = await c.execute(
            text(
                "SELECT count(*) FROM pg_roles WHERE rolname LIKE 'a8r3\\_%' "
                "OR rolname = 'feelings_billing_login'"
            )
        )
        assert left.scalar() == 0
        members = await c.execute(
            text(
                "SELECT count(*) FROM pg_auth_members m WHERE m.roleid = "
                "to_regrole('feelings_billing') OR m.member = to_regrole('feelings_billing')"
            )
        )
        assert members.scalar() == 0
        await c.execute(text(GUARD_R3.GUARD_SQL))


@pytest.mark.parametrize("case", list(R3_ADMIN_HOLES))
async def test_r3_admin_holders_escalate_and_next_run_catches(
    engine: AsyncEngine, case: str
) -> None:
    """К A8-4.4-D4 — что даёт пропущенный ADMIN: страж сейчас пропускает подготовку; роль с
    ADMIN (прямо, по наследованию от группы, роль входа бота с ADMIN, ADMIN на роль входа бота)
    сама выдаёт SET на ``feelings_billing`` себе или ``feelings_api``. Следующий запуск стража
    это ловит — окно открыто до следующего ``alembic upgrade``. Откат."""
    setup, (actor, escalate), target = R3_ADMIN_HOLES[case]
    async with engine.connect() as c:
        tx = await c.begin()
        for sql in setup:
            await c.execute(text(sql))
        await c.execute(text(GUARD_R3.GUARD_SQL))  # пропущено (см. D4)
        assert not await c.scalar(
            text("SELECT pg_has_role(:r, 'feelings_billing', 'SET')"), {"r": target}
        )
        await c.execute(text(f"SET LOCAL ROLE {actor}"))
        await c.execute(text(escalate))
        await c.execute(text("RESET ROLE"))
        assert await c.scalar(
            text("SELECT pg_has_role(:r, 'feelings_billing', 'SET')"), {"r": target}
        ), case
        await c.execute(text("SAVEPOINT g"))
        with pytest.raises(DBAPIError):
            await c.execute(text(GUARD_R3.GUARD_SQL))
        await c.execute(text("ROLLBACK TO SAVEPOINT g"))
        await tx.rollback()
    await _r3_no_leftovers(engine)


@pytest.mark.xfail(
    strict=True,
    reason="A8-4.4-D4: страж допускает ADMIN без SET/INHERIT у любой роли (и ADMIN у роли входа "
    "бота, и любое членство в роли входа бота), а АР-50 п.7 допускает его только у владельца "
    "схемы: такая роль выдаёт себе или feelings_api SET на feelings_billing без суперпользователя",
)
@pytest.mark.parametrize("case", list(R3_ADMIN_HOLES))
async def test_r3_guard_stops_admin_outside_schema_owner(engine: AsyncEngine, case: str) -> None:
    """АР-50 п.7 («ADMIN без SET/INHERIT у владельца схемы допустим»): ADMIN у посторонней роли
    входа, унаследованный от группы, ADMIN у роли входа бота, ADMIN на роль входа бота —
    ожидается остановка ``alembic upgrade head``. Откат."""
    setup, _escalation, _target = R3_ADMIN_HOLES[case]
    stopped = False
    async with engine.connect() as c:
        tx = await c.begin()
        for sql in setup:
            await c.execute(text(sql))
        await c.execute(text("SAVEPOINT g"))
        try:
            await c.run_sync(alembic_on, "upgrade", "head")
        except DBAPIError:
            stopped = True
            await c.execute(text("ROLLBACK TO SAVEPOINT g"))
        await tx.rollback()
    await _r3_no_leftovers(engine)
    assert stopped, case

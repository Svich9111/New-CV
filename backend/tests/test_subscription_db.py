"""4.4, этап 1 (A1): подписка на пару, пробный период, счета и платежи Telegram Stars — миграция
0017.

ФТ-19; ТЗ §7 (``subscriptions``, ``payments``), §11 п.1–7; DECISIONS.md Д6, Д9, АР-13, АР-23,
АР-38, АР-50. Данные заводит суперпользователь (``engine``), действия — под ролью входа
приложения (``sessionmaker`` + ``user_scope``): функции пользователя — с ``app.user_id``, функции
бота — в служебном контексте (``user_scope(…, None)``). Платежи Telegram — заглушки: выдуманные
``telegram_payment_charge_id``, ни одного обращения к Telegram. Данные выдуманные (CLAUDE.md)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import user_scope
from tests.conftest import alembic
from tests.test_couple_functions import (
    ACCEPT,
    LEAVE,
    SM,
    _call,
    _fails,
    _pending,
    _sqlstate,
    _user,
    _wait_lock_waiters,
)

STATUS = "SELECT * FROM app_subscription_status()"
HAS_ACCESS = "SELECT app_has_access()"
TRIAL = "SELECT * FROM app_subscription_start_trial(:d)"
INVOICE = "SELECT * FROM app_subscription_create_invoice(:plan, :amount, :days, :ttl)"
PRECHECK = "SELECT app_bot_payment_precheck(:i, :tg, :amount, :cur)"
CREDIT = "SELECT * FROM app_bot_payment_credit(:i, :tg, :amount, :cur, :ch)"
REFUNDED = "SELECT * FROM app_bot_payment_refunded(:ch)"
LOOKUP = "SELECT * FROM app_bot_payment_lookup(:ch)"
DELETE = "SELECT app_delete_account()"

USER_FUNCS = (
    "app_subscription_status()",
    "app_has_access()",
    "app_subscription_start_trial(integer)",
    "app_subscription_create_invoice(character varying, integer, integer, interval)",
)
BOT_FUNCS = (
    "app_bot_payment_precheck(uuid, bigint, integer, character varying)",
    "app_bot_payment_credit(uuid, bigint, integer, character varying, character varying)",
    "app_bot_payment_refunded(character varying)",
    "app_bot_payment_lookup(character varying)",
)
INTERNAL_FUNCS = (
    "subscription_covered_until(uuid, timestamp with time zone)",
    "subscription_user_has_access(uuid)",
)
ROLES = ("feelings_app", "feelings_api", "feelings_worker", "feelings_admin", "public")
MONTH = {"plan": "month", "amount": 250, "days": 30, "ttl": timedelta(hours=1)}


# --------------------------------------------------------------------------- помощники


async def _rows(sm: SM, uid: uuid.UUID | None, sql: str, **params: Any) -> list[dict[str, Any]]:
    async with user_scope(sm, uid) as s:
        return [dict(r) for r in (await s.execute(text(sql), params)).mappings()]


async def _one(sm: SM, uid: uuid.UUID | None, sql: str, **params: Any) -> dict[str, Any]:
    (row,) = await _rows(sm, uid, sql, **params)
    return row


async def _su(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.begin() as conn:
        res = await conn.execute(text(sql), params)
        return res.scalar_one() if res.returns_rows else None


async def _tg_id(engine: AsyncEngine, uid: uuid.UUID) -> int:
    return int(await _su(engine, "SELECT telegram_id FROM users WHERE id = :u", u=uid))


async def _couple(sm: SM, engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Активная пара (инициатор, партнёр, пара)."""
    a, couple, token = await _pending(sm, engine, "Анна")
    b = await _user(engine, "Игорь")
    await _call(sm, b, ACCEPT, h=token)
    return a, b, couple


async def _invoice(sm: SM, uid: uuid.UUID, **kw: Any) -> uuid.UUID:
    row = await _one(sm, uid, INVOICE, **{**MONTH, **kw})
    return uuid.UUID(str(row["invoice_id"]))


async def _credit(
    sm: SM,
    engine: AsyncEngine,
    invoice: uuid.UUID,
    payer: uuid.UUID,
    charge: str,
    amount: int = 250,
) -> dict[str, Any]:
    tg = await _tg_id(engine, payer)
    return await _one(sm, None, CREDIT, i=invoice, tg=tg, amount=amount, cur="XTR", ch=charge)


def _charge() -> str:
    return f"stub-charge-{uuid.uuid4().hex}"


# --------------------------------------------------------------------------- права и RLS


async def test_function_rights_and_search_path(engine: AsyncEngine) -> None:
    """EXECUTE — только ``feelings_app`` (служебные функции — без ролей); SECURITY DEFINER с
    ``search_path = public, pg_temp``; прямой записи в подписки и платежи у ролей нет."""
    async with engine.connect() as c:

        async def can(role: str, f: str) -> bool:
            q = text("SELECT has_function_privilege(:r, :f, 'EXECUTE')")
            return bool((await c.execute(q, {"r": role, "f": f})).scalar_one())

        for f in USER_FUNCS + BOT_FUNCS:
            assert await can("feelings_app", f), f
            for role in ROLES[1:]:
                assert not await can(role, f), (role, f)
        for f in INTERNAL_FUNCS:
            for role in ROLES:
                assert not await can(role, f), (role, f)
        rows = await c.execute(
            text(
                "SELECT p.proname AS f, p.prosecdef, p.proconfig FROM pg_proc p "
                "WHERE p.oid = ANY(CAST(:f AS regprocedure[]))"
            ),
            {"f": list(USER_FUNCS + BOT_FUNCS + INTERNAL_FUNCS)},
        )
        internal = {f.split("(")[0] for f in INTERNAL_FUNCS}
        seen = 0
        for r in rows:
            seen += 1
            assert r.prosecdef is (r.f not in internal), r.f
            assert "search_path=public, pg_temp" in (r.proconfig or []), r
        assert seen == len(USER_FUNCS + BOT_FUNCS + INTERNAL_FUNCS)
        for table in ("subscriptions", "payments"):
            for priv in ("INSERT", "UPDATE", "DELETE"):
                for role in ROLES[:4]:
                    q = text("SELECT has_table_privilege(:r, :t, :p)")
                    assert not (
                        await c.execute(q, {"r": role, "t": table, "p": priv})
                    ).scalar_one(), (role, table, priv)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE subscriptions SET period_end = now() + interval '10 years'",
        "INSERT INTO subscriptions (couple_id, plan, status, period_start, period_end) "
        "VALUES (:c, 'trial', 'trial', now(), now() + interval '1 day')",
        "UPDATE payments SET status = 'succeeded'",
        "DELETE FROM payments",
    ],
)
async def test_members_cannot_write_directly(
    sessionmaker: SM, engine: AsyncEngine, sql: str
) -> None:
    a, _b, couple = await _couple(sessionmaker, engine)
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with user_scope(sessionmaker, a) as s:
            await s.execute(text(sql), {"c": couple})


async def test_rls_partners_see_status_outsider_does_not(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """ФТ-19, §11 п.1: статус и оплату видят оба партнёра; посторонний — ничего; ожидающий счёт
    напрямую не виден никому (только функциям)."""
    a, b, couple = await _couple(sessionmaker, engine)
    olga = await _user(engine, "Ольга")
    await _one(sessionmaker, a, TRIAL, d=7)
    inv = await _invoice(sessionmaker, b)
    pending = await _invoice(sessionmaker, a, plan="year", amount=2000, days=365)
    await _credit(sessionmaker, engine, inv, b, _charge())
    for who in (a, b):
        async with user_scope(sessionmaker, who) as s:
            subs = (
                await s.execute(
                    text("SELECT plan FROM subscriptions WHERE couple_id = :c"), {"c": couple}
                )
            ).scalars()
            assert sorted(subs) == ["month", "trial"]
            pays = (await s.execute(text("SELECT id FROM payments"))).scalars().all()
            assert pays == [inv] and pending not in pays
        st = await _one(sessionmaker, who, STATUS)
        # Сейчас идёт пробный период; оплаченный начнётся после него.
        assert (st["status"], st["plan"], st["couple_id"]) == ("trial", "trial", couple)
        assert st["paid_by_me"] is (who == b)
    async with user_scope(sessionmaker, olga) as s:
        for t in ("subscriptions", "payments"):
            assert (await s.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one() == 0
    st = await _one(sessionmaker, olga, STATUS)
    assert (st["status"], st["couple_id"], st["has_access"]) == ("none", None, False)


async def test_user_and_service_contexts_are_separated(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Функции бота — только без ``app.user_id`` (сессия пользователя не зачислит и не вернёт
    платёж); функции пользователя — только с ним; неизвестный пользователь — 42501."""
    a, _b, _ = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    tg = await _tg_id(engine, a)
    service: tuple[tuple[str, dict[str, Any]], ...] = (
        (PRECHECK, {"i": inv, "tg": tg, "amount": 250, "cur": "XTR"}),
        (CREDIT, {"i": inv, "tg": tg, "amount": 250, "cur": "XTR", "ch": _charge()}),
        (REFUNDED, {"ch": _charge()}),
        (LOOKUP, {"ch": _charge()}),
    )
    for sql, params in service:
        await _fails(sessionmaker, a, "42501", sql, **params)
    user: tuple[tuple[str, dict[str, Any]], ...] = (
        (STATUS, {}), (HAS_ACCESS, {}), (TRIAL, {"d": 7}), (INVOICE, MONTH),
    )  # fmt: skip
    for sql, params in user:
        await _fails(sessionmaker, None, "42501", sql, **params)
        await _fails(sessionmaker, uuid.uuid4(), "42501", sql, **params)


# --------------------------------------------------------------------------- пробный период


async def test_status_before_couple_and_while_pending(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """§11 п.3: до принятия приглашения подписки нет (бесплатный дневной лимит — API), пробный
    период не начинается: FC001."""
    loner = await _user(engine, "Лев")
    st = await _one(sessionmaker, loner, STATUS)
    assert st == {
        "couple_id": None, "couple_status": None, "status": "none", "plan": None,
        "period_end": None, "trial_ends_at": None, "trial_used": False, "paid_by_me": None,
        "has_access": False,
    }  # fmt: skip
    await _fails(sessionmaker, loner, "FC001", TRIAL, d=7)
    await _fails(sessionmaker, loner, "FC001", INVOICE, **MONTH)
    vera, couple, _ = await _pending(sessionmaker, engine)
    st = await _one(sessionmaker, vera, STATUS)
    assert (st["couple_id"], st["couple_status"], st["status"]) == (couple, "pending", "none")
    await _fails(sessionmaker, vera, "FC001", TRIAL, d=7)
    await _fails(sessionmaker, vera, "FC001", INVOICE, **MONTH)
    assert await _call(sessionmaker, vera, HAS_ACCESS) is False


async def test_trial_once_per_couple_from_activation(sessionmaker: SM, engine: AsyncEngine) -> None:
    """§11 п.2 (Д6): пробный период от активации пары, один раз на пару — повтор любым партнёром
    отдаёт ту же строку; оба видят статус ``trial``; запись в audit_log без текстов."""
    a, b, couple = await _couple(sessionmaker, engine)
    activated = await _su(engine, "SELECT activated_at FROM couples WHERE id = :c", c=couple)
    first = await _one(sessionmaker, a, TRIAL, d=7)
    assert first["created"] is True and first["period_end"] == activated + timedelta(days=7)
    again = await _one(sessionmaker, b, TRIAL, d=14)
    assert again == {**first, "created": False}
    for who in (a, b):
        st = await _one(sessionmaker, who, STATUS)
        assert (st["status"], st["plan"], st["period_end"]) == (
            "trial",
            "trial",
            first["period_end"],
        )
        assert st["trial_used"] is True and st["trial_ends_at"] == first["period_end"]
        assert st["paid_by_me"] is None and st["has_access"] is True
        assert await _call(sessionmaker, who, HAS_ACCESS) is True
    assert (
        await _su(engine, "SELECT count(*) FROM subscriptions WHERE couple_id = :c", c=couple) == 1
    )
    audit = await _su(
        engine,
        "SELECT string_agg(action || ':' || object_type || ':' || (actor_id = :a)::text, ',') "
        "FROM audit_log WHERE object_id = :s",
        a=a,
        s=first["subscription_id"],
    )
    assert audit == "subscription.trial:subscription:true"
    with pytest.raises(DBAPIError) as ei:  # индекс держит инвариант и в обход функции
        await _su(
            engine,
            "INSERT INTO subscriptions (couple_id, plan, status, period_start, period_end) "
            "VALUES (:c, 'trial', 'trial', now(), now() + interval '1 day')",
            c=couple,
        )
    assert _sqlstate(ei.value) == "23505"
    for bad in (0, 31, None):
        await _fails(sessionmaker, a, "22023", TRIAL, d=bad)


async def test_concurrent_trial_creates_one(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Два одновременных вызова (оба партнёра) ждут блокировку пары: создан ровно один."""
    a, b, couple = await _couple(sessionmaker, engine)
    async with engine.connect() as holder:
        tx = await holder.begin()
        await holder.execute(text("SELECT 1 FROM couples WHERE id = :c FOR UPDATE"), {"c": couple})
        calls = [asyncio.create_task(_one(sessionmaker, u, TRIAL, d=7)) for u in (a, b)]
        await _wait_lock_waiters(engine, "app_subscription_start_trial", 2)
        await tx.rollback()
    res = await asyncio.gather(*calls)
    assert sorted(r["created"] for r in res) == [False, True]
    assert res[0]["subscription_id"] == res[1]["subscription_id"]


async def test_late_trial_is_counted_from_activation_and_expires(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Поздний вызов не продлевает пробный период: истёкший — записан и отмечен использованным;
    статус ``expired``, доступа нет."""
    a, b, couple = await _couple(sessionmaker, engine)
    await _su(
        engine,
        "UPDATE couples SET activated_at = now() - interval '10 days' WHERE id = :c",
        c=couple,
    )
    t = await _one(sessionmaker, b, TRIAL, d=7)
    assert t["created"] is True and t["period_end"] < datetime.now(UTC)
    st = await _one(sessionmaker, a, STATUS)
    assert (st["status"], st["plan"], st["trial_used"], st["has_access"]) == (
        "expired", "trial", True, False,
    )  # fmt: skip
    assert await _call(sessionmaker, a, HAS_ACCESS) is False


# --------------------------------------------------------------------------- счета


async def test_invoice_rules(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Счёт: строка ``pending`` XTR без периода и без личных данных; переиспользование;
    проверка параметров; предел действующих счетов пары (FC002 → 429)."""
    a, b, couple = await _couple(sessionmaker, engine)
    first = await _one(sessionmaker, a, INVOICE, **MONTH)
    assert first["reused"] is False
    row = await _su(
        engine,
        "SELECT row_to_json(p)::text FROM (SELECT couple_id, subscription_id, amount, currency, "
        "provider, provider_charge_id, status, plan, period_days FROM payments WHERE id = :i) p",
        i=first["invoice_id"],
    )
    assert row == (
        f'{{"couple_id":"{couple}","subscription_id":null,"amount":250,"currency":"XTR",'
        '"provider":"telegram_stars","provider_charge_id":null,"status":"pending",'
        '"plan":"month","period_days":30}'
    )
    again = await _one(sessionmaker, b, INVOICE, **MONTH)  # партнёр, те же условия
    assert again == {**first, "reused": True}
    other = await _one(sessionmaker, a, INVOICE, **{**MONTH, "amount": 300})
    assert other["reused"] is False
    for bad in (
        {"plan": "week"}, {"plan": "trial"}, {"amount": 0}, {"amount": 100001}, {"days": 0},
        {"days": 401}, {"ttl": timedelta(0)}, {"ttl": timedelta(days=2)}, {"plan": None},
    ):  # fmt: skip
        await _fails(sessionmaker, a, "22023", INVOICE, **{**MONTH, **bad})
    for n in range(18):  # всего 20 действующих
        await _one(sessionmaker, a, INVOICE, **{**MONTH, "amount": 1000 + n})
    await _fails(sessionmaker, b, "FC002", INVOICE, **{**MONTH, "amount": 5000})
    # Истёкшие больше суток назад и не оплаченные — удаляются при следующем счёте.
    await _su(
        engine,
        "UPDATE payments SET created_at = now() - interval '3 days', "
        "expires_at = now() - interval '2 days' WHERE couple_id = :c",
        c=couple,
    )
    await _one(sessionmaker, a, INVOICE, **MONTH)
    assert await _su(engine, "SELECT count(*) FROM payments WHERE couple_id = :c", c=couple) == 1


async def test_precheck_codes(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Ответ на pre_checkout_query: каждый отказ — свой код, только чтение."""
    a, b, _couple_id = await _couple(sessionmaker, engine)
    olga = await _user(engine, "Ольга")
    inv = await _invoice(sessionmaker, a)
    tg_b, tg_o = await _tg_id(engine, b), await _tg_id(engine, olga)

    async def check(i: uuid.UUID, tg: int, amount: int = 250, cur: str = "XTR") -> str:
        return str(await _call(sessionmaker, None, PRECHECK, i=i, tg=tg, amount=amount, cur=cur))

    assert await check(inv, tg_b) == "ok"  # платит любой из партнёров (§11 п.1)
    assert await check(uuid.uuid4(), tg_b) == "invoice_not_found"
    assert await check(inv, tg_b, amount=249) == "amount_mismatch"
    assert await check(inv, tg_b, cur="RUB") == "amount_mismatch"
    assert await check(inv, tg_o) == "not_member"
    assert await check(inv, 999_999_999_999) == "not_member"
    expired = await _invoice(sessionmaker, a, amount=10)
    await _su(
        engine,
        "UPDATE payments SET expires_at = now() - interval '1 second' WHERE id = :i",
        i=expired,
    )
    assert await check(expired, tg_b, amount=10) == "invoice_expired"
    await _credit(sessionmaker, engine, inv, b, _charge())
    assert await check(inv, tg_b) == "invoice_used"
    second = await _invoice(sessionmaker, a, amount=11)
    await _call(sessionmaker, b, LEAVE)
    assert await check(second, tg_b, amount=11) == "couple_not_active"
    assert await _su(engine, "SELECT status FROM payments WHERE id = :i", i=second) == "pending"


# --------------------------------------------------------------------------- зачисление


async def test_credit_stacks_after_trial_and_is_idempotent(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Оплата во время пробного периода начинается после него; повтор того же
    ``telegram_payment_charge_id`` — ``duplicate`` без изменений; плательщик — платящий."""
    a, b, couple = await _couple(sessionmaker, engine)
    trial = await _one(sessionmaker, a, TRIAL, d=7)
    inv = await _invoice(sessionmaker, a)
    charge = _charge()
    first = await _credit(sessionmaker, engine, inv, b, charge)
    assert first["result"] == "credited" and first["payment_id"] == inv
    assert first["period_end"] == trial["period_end"] + timedelta(days=30)
    sub = await _su(
        engine,
        "SELECT row_to_json(s)::text FROM (SELECT payer_id, plan, status, period_start "
        "FROM subscriptions WHERE id = :s) s",
        s=first["subscription_id"],
    )
    assert f'"payer_id":"{b}","plan":"month","status":"active"' in sub
    pay = await _su(
        engine,
        "SELECT status || '|' || provider_charge_id || '|' || (paid_at IS NOT NULL)::text "
        "FROM payments WHERE id = :i",
        i=inv,
    )
    assert pay == f"succeeded|{charge}|true"
    before = await _su(engine, "SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM "
                       "(SELECT * FROM subscriptions WHERE couple_id = :c) t", c=couple)  # fmt: skip
    for _ in range(2):
        dup = await _credit(sessionmaker, engine, inv, b, charge)
        assert dup == {**first, "result": "duplicate"}
    after = await _su(engine, "SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM "
                      "(SELECT * FROM subscriptions WHERE couple_id = :c) t", c=couple)  # fmt: skip
    assert after == before
    st = await _one(sessionmaker, a, STATUS)
    # Сейчас идёт пробный период, доступ — до конца оплаченного; платил партнёр.
    assert (st["status"], st["period_end"], st["paid_by_me"]) == (
        "trial",
        first["period_end"],
        False,
    )
    assert (await _one(sessionmaker, b, STATUS))["paid_by_me"] is True
    audit = await _su(
        engine,
        "SELECT count(*) FROM audit_log WHERE action = 'payment.credit' AND object_id = :i "
        "AND actor_id = :b",
        i=inv,
        b=b,
    )
    assert audit == 1


async def test_concurrent_duplicate_credit(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Две одновременные доставки одного successful_payment: ``credited`` и ``duplicate``,
    один период; проверка повтора — после блокировки пары."""
    a, _b, couple = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    tg = await _tg_id(engine, a)
    charge = _charge()
    async with engine.connect() as holder:
        tx = await holder.begin()
        await holder.execute(text("SELECT 1 FROM couples WHERE id = :c FOR UPDATE"), {"c": couple})
        calls = [
            asyncio.create_task(
                _one(sessionmaker, None, CREDIT, i=inv, tg=tg, amount=250, cur="XTR", ch=charge)
            )
            for _ in range(2)
        ]
        await _wait_lock_waiters(engine, "app_bot_payment_credit", 2)
        await tx.rollback()
    res = await asyncio.gather(*calls)
    assert sorted(r["result"] for r in res) == ["credited", "duplicate"]
    assert res[0]["subscription_id"] == res[1]["subscription_id"]
    assert (
        await _su(engine, "SELECT count(*) FROM subscriptions WHERE couple_id = :c", c=couple) == 1
    )


async def test_second_payment_of_same_invoice_and_rejections(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Гонка двух pre_checkout одного счёта: второй платёж — новая строка и ещё период.
    Неизвестный счёт и несовпавшая сумма — без зачисления; неизвестный плательщик — период без
    ``payer_id``."""
    a, b, couple = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    one = await _credit(sessionmaker, engine, inv, a, _charge())
    two = await _credit(sessionmaker, engine, inv, b, _charge())
    assert (one["result"], two["result"]) == ("credited", "credited")
    assert two["payment_id"] != inv
    assert two["period_end"] == one["period_end"] + timedelta(days=30)
    n = await _su(
        engine,
        "SELECT count(*) FROM payments WHERE couple_id = :c AND status = 'succeeded'",
        c=couple,
    )
    assert n == 2
    for kw, expected in (
        ({"i": uuid.uuid4(), "amount": 250}, "invoice_not_found"),
        ({"i": inv, "amount": 1}, "amount_mismatch"),
    ):
        r = await _one(sessionmaker, None, CREDIT, tg=1, cur="XTR", ch=_charge(), **kw)
        assert (r["result"], r["payment_id"]) == (expected, None)
    stranger = await _one(
        sessionmaker, None, CREDIT, i=inv, tg=123_456_789_012, amount=250, cur="XTR", ch=_charge()
    )
    assert stranger["result"] == "credited"
    payer = await _su(
        engine, "SELECT payer_id FROM subscriptions WHERE id = :s", s=stranger["subscription_id"]
    )
    assert payer is None
    for bad in ("", "x" * 129, None):
        await _fails(
            sessionmaker, None, "22023", CREDIT, i=inv, tg=1, amount=250, cur="XTR", ch=bad
        )
    await _fails(
        sessionmaker, None, "22023", CREDIT, i=inv, tg=1, amount=0, cur="XTR", ch=_charge()
    )


# --------------------------------------------------------------------------- возврат


async def test_refund_cancels_period(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Возврат: платёж ``refunded``, период ``canceled``, доступ пропадает; повтор —
    ``already_refunded``; данные для refundStarPayment — Telegram ID плательщика."""
    a, b, _ = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    charge = _charge()
    paid = await _credit(sessionmaker, engine, inv, a, charge)
    assert await _call(sessionmaker, b, HAS_ACCESS) is True
    look = await _one(sessionmaker, None, LOOKUP, ch=charge)
    assert (look["payment_id"], look["status"], look["amount"], look["currency"]) == (
        inv, "succeeded", 250, "XTR",
    )  # fmt: skip
    assert look["payer_telegram_id"] == await _tg_id(engine, a)
    assert await _rows(sessionmaker, None, LOOKUP, ch=_charge()) == []
    r = await _one(sessionmaker, None, REFUNDED, ch=charge)
    assert r == {"result": "refunded", "payment_id": inv}
    assert await _one(sessionmaker, None, REFUNDED, ch=charge) == {
        "result": "already_refunded", "payment_id": inv,
    }  # fmt: skip
    assert (await _one(sessionmaker, None, REFUNDED, ch=_charge()))["result"] == "not_found"
    state = await _su(
        engine,
        "SELECT p.status || '|' || (p.refunded_at IS NOT NULL)::text || '|' || s.status "
        "FROM payments p JOIN subscriptions s ON s.id = p.subscription_id WHERE p.id = :i",
        i=inv,
    )
    assert state == "refunded|true|canceled"
    for who in (a, b):
        st = await _one(sessionmaker, who, STATUS)
        assert (st["status"], st["has_access"], st["paid_by_me"]) == ("canceled", False, None)
    assert paid["subscription_id"]
    assert await _su(
        engine, "SELECT count(*) FROM audit_log WHERE action = 'payment.refund' AND object_id = :i", i=inv
    ) == 1  # fmt: skip


# --------------------------------------------------------------------------- распад и удаление


async def test_dissolution_keeps_paid_access_until_period_end(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """§11 п.6: после выхода оплаченный период дослуживает обоим (личный чат), пробный —
    нет; текущей пары нет — статус ``none``; оплата после распада — ``couple_ended``."""
    a, b, couple = await _couple(sessionmaker, engine)
    await _one(sessionmaker, a, TRIAL, d=7)
    inv = await _invoice(sessionmaker, a)
    await _call(sessionmaker, a, LEAVE)
    for who in (a, b):
        assert await _call(sessionmaker, who, HAS_ACCESS) is False  # только пробный — доступа нет
    paid = await _credit(sessionmaker, engine, inv, b, _charge())  # pre_checkout прошёл до выхода
    assert paid["result"] == "couple_ended"
    for who in (a, b):
        assert await _call(sessionmaker, who, HAS_ACCESS) is True
        st = await _one(sessionmaker, who, STATUS)
        assert (st["status"], st["couple_id"], st["has_access"]) == ("none", None, True)
    # Новая пара — свой пробный период (АР-50, вопрос о злоупотреблении).
    _c, _new, token = await _pending(sessionmaker, engine, "Вера")
    await _call(sessionmaker, a, ACCEPT, h=token)
    assert (await _one(sessionmaker, a, TRIAL, d=7))["created"] is True
    await _su(
        engine,
        "UPDATE subscriptions SET period_start = now() - interval '40 days', "
        "period_end = now() - interval '1 second' WHERE couple_id = :c",
        c=couple,
    )
    assert await _call(sessionmaker, b, HAS_ACCESS) is False


async def test_account_deletion(sessionmaker: SM, engine: AsyncEngine) -> None:
    """4.5, АР-23 (а): удаление плательщика — ``payer_id`` NULL, платёж и период пары остаются
    (Telegram ID для возврата больше не выдаётся); удаление второго — пара с периодами, счетами и
    платежами удаляется каскадом."""
    a, b, couple = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    charge = _charge()
    paid = await _credit(sessionmaker, engine, inv, a, charge)
    pending = await _invoice(sessionmaker, b, amount=99)
    await _call(sessionmaker, a, DELETE)
    assert (
        await _su(
            engine, "SELECT payer_id FROM subscriptions WHERE id = :s", s=paid["subscription_id"]
        )
        is None
    )
    assert (await _one(sessionmaker, None, LOOKUP, ch=charge))["payer_telegram_id"] is None
    assert await _call(sessionmaker, b, HAS_ACCESS) is True  # оплаченный период распавшейся пары
    await _call(sessionmaker, b, DELETE)
    for sql in (
        "SELECT count(*) FROM couples WHERE id = :c",
        "SELECT count(*) FROM subscriptions WHERE couple_id = :c",
        "SELECT count(*) FROM payments WHERE couple_id = :c",
    ):
        assert await _su(engine, sql, c=couple) == 0, sql
    assert (
        await _su(engine, "SELECT count(*) FROM payments WHERE id IN (:i, :p)", i=inv, p=pending)
        == 0
    )
    r = await _one(sessionmaker, None, CREDIT, i=pending, tg=1, amount=99, cur="XTR", ch=_charge())
    assert r["result"] == "invoice_not_found"


async def test_credit_waits_for_partner_account_deletion_without_deadlock(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """АР-13/АР-38: зачисление (пользователь → пара) и удаление аккаунта партнёра (пользователь →
    приглашения → пара) идут по очереди, без 40P01: удаление держит пару — зачисление ждёт и
    записывает период распавшейся пары (``couple_ended``)."""
    a, b, couple = await _couple(sessionmaker, engine)
    inv = await _invoice(sessionmaker, a)
    tg = await _tg_id(engine, a)
    async with engine.connect() as holder:
        tx = await holder.begin()
        await holder.execute(text("SELECT 1 FROM couples WHERE id = :c FOR UPDATE"), {"c": couple})
        deleting = asyncio.create_task(_call(sessionmaker, b, DELETE))
        await _wait_lock_waiters(engine, "app_delete_account", 1)
        crediting = asyncio.create_task(
            _one(sessionmaker, None, CREDIT, i=inv, tg=tg, amount=250, cur="XTR", ch=_charge())
        )
        await _wait_lock_waiters(engine, "app_bot_payment_credit", 1)
        await tx.rollback()
    await deleting
    res = await crediting
    assert res["result"] == "couple_ended"
    assert await _call(sessionmaker, a, HAS_ACCESS) is True


# --------------------------------------------------------------------------- обратимость


async def test_migration_0017_reversible(
    sessionmaker: SM, engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """head → 0016 → head: функции и колонки уходят и возвращаются с правами; периоды и
    оплаченные платежи сохраняются, не оплаченные счета удаляются; ``alembic check`` чист."""
    a, _b, couple = await _couple(sessionmaker, engine)
    await _one(sessionmaker, a, TRIAL, d=7)
    inv = await _invoice(sessionmaker, a)
    await _credit(sessionmaker, engine, inv, a, _charge())
    pending = await _invoice(sessionmaker, a, amount=77)
    subs = await _su(
        engine,
        "SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM "
        "(SELECT * FROM subscriptions WHERE couple_id = :c) t",
        c=couple,
    )
    try:
        alembic("downgrade", "0016")
        await engine.dispose()
        async with engine.connect() as c:
            for f in USER_FUNCS + BOT_FUNCS + INTERNAL_FUNCS:
                assert (
                    await c.execute(text("SELECT to_regprocedure(:f)"), {"f": f})
                ).scalar() is None
            cols = (
                await c.execute(
                    text(
                        "SELECT string_agg(column_name, ',' ORDER BY column_name) FROM "
                        "information_schema.columns WHERE table_name = 'payments'"
                    )
                )
            ).scalar()
            assert (
                cols
                == "amount,created_at,currency,id,provider,provider_charge_id,status,subscription_id"
            )
            assert (
                await c.execute(text("SELECT count(*) FROM payments WHERE id = :i"), {"i": inv})
            ).scalar() == 1
            assert (
                await c.execute(text("SELECT count(*) FROM payments WHERE id = :i"), {"i": pending})
            ).scalar() == 0
        alembic("upgrade", "head")
        await engine.dispose()
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    assert await _su(
        engine,
        "SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM "
        "(SELECT * FROM subscriptions WHERE couple_id = :c) t",
        c=couple,
    ) == subs  # fmt: skip
    async with engine.connect() as c:
        for f in USER_FUNCS + BOT_FUNCS:
            q = text("SELECT has_function_privilege('feelings_app', :f, 'EXECUTE')")
            assert (await c.execute(q, {"f": f})).scalar_one() is True, f
    st = await _one(sessionmaker, a, STATUS)
    assert st["status"] == "trial"
    alembic("check")

"""4.5, этап 2: функции выгрузки app_export_partner_messages() (миграция 0006) и
app_export_safety_events() (миграция 0007).

ФТ-21, ФТ-10; ТЗ §2 п.3, §7, §9.1 п.6, §9.3; DECISIONS.md АР-11, АР-13, АР-25.
Данные заводит суперпользователь (`engine`) или слой доступа (шифрование 4.2), выгрузка — под
ролью входа приложения `feelings_api` (`user_scope` → `feelings_app`). Данные выдуманные."""

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core import crypto
from app.core.crypto import Keyring, generate_key_entry, parse_keyring
from app.db import user_scope
from tests.conftest import alembic
from tests.test_account_delete import Person, _couple, _person, _send_encrypted
from tests.test_couple_functions import SM, _call, _sqlstate

EXPORT = "SELECT * FROM app_export_partner_messages()"
RECEIVED = ("delivered", "read", "answered")


def _keys() -> Keyring:
    return parse_keyring(generate_key_entry("k1"), "k1")


async def _export(sm: SM, uid: uuid.UUID | None) -> list[dict[str, Any]]:
    async with user_scope(sm, uid) as s:
        return [dict(r._mapping) for r in await s.execute(text(EXPORT))]


async def _export_fails(sm: SM, uid: uuid.UUID | None) -> str | None:
    with pytest.raises(DBAPIError) as ei:
        await _export(sm, uid)
    return _sqlstate(ei.value)


async def _deliver(
    engine: AsyncEngine, keys: Keyring, msg: uuid.UUID, body: str, *, adapted: bool
) -> None:
    """Конвейер (под feelings_worker): версия для получателя с контекстом сообщения."""
    async with engine.connect() as conn, conn.begin():
        await conn.execute(text("SET LOCAL ROLE feelings_worker"))
        ok = (
            await conn.execute(
                text("SELECT pipeline_deliver(:m, :t, :ad, 2::smallint, false)"),
                {"m": msg, "t": keys.seal(crypto.MESSAGE_ADAPTED, msg, body), "ad": adapted},
            )
        ).scalar_one()
    assert ok is True


async def _withhold(engine: AsyncEngine, msg: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(text("SET LOCAL ROLE feelings_worker"))
        assert (
            await conn.execute(text("SELECT pipeline_withhold(:m)"), {"m": msg})
        ).scalar_one() is True


async def _viewed_at(engine: AsyncEngine, msg: uuid.UUID) -> object:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT original_viewed_at FROM partner_messages WHERE id = :m"), {"m": msg}
            )
        ).scalar_one()


class Scene:
    """Анна и Игорь в активной паре, посторонняя Ольга. У каждого из пары по сообщению в
    статусах delivered (адаптированное и нет), withheld и processing."""

    anna: Person
    igor: Person
    olga: Person
    couple: uuid.UUID
    msgs: dict[str, tuple[uuid.UUID, uuid.UUID]]  # метка → (draft_id, message_id)
    texts: dict[str, str]


async def _scene(sm: SM, engine: AsyncEngine, keys: Keyring) -> Scene:
    sc = Scene()
    sc.anna, sc.igor = await _person(engine, "Анна"), await _person(engine, "Игорь")
    sc.olga = await _person(engine, "Ольга")
    sc.couple, _ = await _couple(sm, sc.anna, sc.igor)
    sc.msgs, sc.texts = {}, {}
    for who, person in (("a", sc.anna), ("i", sc.igor)):
        for kind in ("adapted", "plain", "withheld", "processing"):
            label = f"{who}-{kind}"
            sc.texts[label] = f"оригинал {label} {uuid.uuid4().hex}"
            sc.msgs[label] = await _send_encrypted(sm, keys, person, sc.texts[label])
            msg = sc.msgs[label][1]
            if kind == "adapted":
                await _deliver(engine, keys, msg, f"мягко {label}", adapted=True)
            elif kind == "plain":
                await _deliver(engine, keys, msg, sc.texts[label], adapted=False)
            elif kind == "withheld":
                await _withhold(engine, msg)
    return sc


def _ids(rows: list[dict[str, Any]], outgoing: bool) -> set[uuid.UUID]:
    return {r["id"] for r in rows if r["outgoing"] is outgoing}


async def test_export_own_sent_and_delivered_received(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Свои отправленные — во всех статусах, с оригиналом и id черновика для AAD, без версии
    для получателя; полученные — только delivered/read/answered: показанная версия и оригинал."""
    keys = _keys()
    sc = await _scene(sessionmaker, engine, keys)
    rows = await _export(sessionmaker, sc.anna.user_id)
    m = sc.msgs
    assert _ids(rows, True) == {
        m[f"a-{k}"][1] for k in ("adapted", "plain", "withheld", "processing")
    }
    assert _ids(rows, False) == {m["i-adapted"][1], m["i-plain"][1]}
    assert [r["sent_at"] for r in rows] == sorted(r["sent_at"] for r in rows)
    by_id = {r["id"]: r for r in rows}
    for kind in ("adapted", "plain", "withheld", "processing"):
        draft, msg = m[f"a-{kind}"]
        r = by_id[msg]
        assert r["draft_id"] == draft and r["couple_id"] == sc.couple
        assert keys.open(crypto.MESSAGE_ORIGINAL, draft, r["original_enc"]) == sc.texts[f"a-{kind}"]
        # Версия, которую агент Игоря сделал для Игоря, автору не отдаётся (АР-25 п.3).
        assert r["shown_enc"] is None and r["shown_is_original"] is None
        assert r["original_viewed_at"] is None
    assert by_id[m["a-adapted"][1]]["is_adapted"] is True
    assert by_id[m["a-withheld"][1]]["status"] == "withheld"
    assert by_id[m["a-processing"][1]]["status"] == "processing"
    for kind, shown in (("adapted", "мягко i-adapted"), ("plain", sc.texts["i-plain"])):
        draft, msg = m[f"i-{kind}"]
        r = by_id[msg]
        assert r["draft_id"] == draft and r["shown_is_original"] is False
        assert keys.open(crypto.MESSAGE_ADAPTED, msg, r["shown_enc"]) == shown
        assert keys.open(crypto.MESSAGE_ORIGINAL, draft, r["original_enc"]) == sc.texts[f"i-{kind}"]
        assert r["is_adapted"] is (kind == "adapted")
        assert r["status"] == "delivered" and r["original_viewed_at"] is not None
    assert "tone_score" not in rows[0]
    # Шифротекст версии для Игоря (сообщение Анны) не встречается ни в одной колонке выгрузки.
    async with engine.connect() as conn:
        adapted = (
            await conn.execute(
                text("SELECT adapted_enc FROM partner_messages WHERE id = :m"),
                {"m": m["a-adapted"][1]},
            )
        ).scalar_one()
    assert adapted is not None
    assert all(adapted != v for r in rows for v in r.values())


async def test_isolation_partner_stranger_anonymous(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Игорь получает свои сообщения и только доставленные ему сообщения Анны; посторонняя —
    ничего; без входа и для помеченного удалённым — 42501."""
    keys = _keys()
    sc = await _scene(sessionmaker, engine, keys)
    m = sc.msgs
    igor = await _export(sessionmaker, sc.igor.user_id)
    assert _ids(igor, True) == {
        m[f"i-{k}"][1] for k in ("adapted", "plain", "withheld", "processing")
    }
    assert _ids(igor, False) == {m["a-adapted"][1], m["a-plain"][1]}
    # Ни один оригинал остановленного или ещё не доставленного сообщения Анны у Игоря.
    hidden = {m["a-withheld"][1], m["a-processing"][1]}
    assert not hidden & {r["id"] for r in igor}
    assert await _export(sessionmaker, sc.olga.user_id) == []
    assert await _export_fails(sessionmaker, None) == "42501"
    assert await _export_fails(sessionmaker, uuid.uuid4()) == "42501"
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": sc.olga.user_id}
        )
    assert await _export_fails(sessionmaker, sc.olga.user_id) == "42501"


async def test_export_fixes_original_view(sessionmaker: SM, engine: AsyncEngine) -> None:
    """ТЗ §7, АР-25 п.2: выдача оригинала фиксируется (COALESCE — первая отметка не меняется);
    у отправленных и у недоставленных отметка не ставится; автор её в выгрузке не видит."""
    keys = _keys()
    sc = await _scene(sessionmaker, engine, keys)
    m = sc.msgs
    received = m["i-adapted"][1]
    assert await _viewed_at(engine, received) is None
    first = {r["id"]: r for r in await _export(sessionmaker, sc.anna.user_id)}
    stamp = await _viewed_at(engine, received)
    assert stamp is not None and first[received]["original_viewed_at"] == stamp
    again = {r["id"]: r for r in await _export(sessionmaker, sc.anna.user_id)}
    assert again[received]["original_viewed_at"] == stamp == await _viewed_at(engine, received)
    for label in ("i-withheld", "i-processing", "a-adapted", "a-plain"):
        assert await _viewed_at(engine, m[label][1]) is None, label
    igor = {r["id"]: r for r in await _export(sessionmaker, sc.igor.user_id)}
    assert igor[received]["original_viewed_at"] is None  # действие Анны автору не выдаётся


async def test_received_without_recipient_version(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Доставленное без версии для получателя (adapted_enc NULL) — показана версия-оригинал:
    shown_enc = original_enc, shown_is_original = true (расшифровка с контекстом черновика)."""
    keys = _keys()
    anna, igor = await _person(engine, "Анна"), await _person(engine, "Игорь")
    await _couple(sessionmaker, anna, igor)
    draft, msg = await _send_encrypted(sessionmaker, keys, igor, "как есть")
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE partner_messages SET status = 'read', read_at = now() WHERE id = :m"),
            {"m": msg},
        )
    (row,) = await _export(sessionmaker, anna.user_id)
    assert row["shown_is_original"] is True and row["shown_enc"] == row["original_enc"]
    assert keys.open(crypto.MESSAGE_ORIGINAL, draft, row["shown_enc"]) == "как есть"
    assert row["status"] == "read" and row["read_at"] is not None


async def test_export_after_leaving_couple(sessionmaker: SM, engine: AsyncEngine) -> None:
    """После выхода из пары история распавшейся пары остаётся в выгрузке обоих (ТЗ §7)."""
    keys = _keys()
    sc = await _scene(sessionmaker, engine, keys)
    await _call(sessionmaker, sc.anna.user_id, "SELECT app_leave_couple()")
    rows = await _export(sessionmaker, sc.anna.user_id)
    assert len(_ids(rows, True)) == 4 and len(_ids(rows, False)) == 2
    assert {r["couple_id"] for r in rows} == {sc.couple}


async def test_export_after_partner_deleted(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Партнёр удалил аккаунт (АР-23): его сообщения удалены, свои остаются с оригиналом."""
    keys = _keys()
    sc = await _scene(sessionmaker, engine, keys)
    await _call(sessionmaker, sc.igor.user_id, "SELECT app_delete_account()")
    rows = await _export(sessionmaker, sc.anna.user_id)
    assert _ids(rows, False) == set() and len(_ids(rows, True)) == 4
    for r in rows:
        assert keys.open(crypto.MESSAGE_ORIGINAL, r["draft_id"], r["original_enc"])
        assert r["is_adapted"] is False  # версия для удалённого получателя обнулена


async def test_export_races_partner_delete_without_deadlock(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """АР-13: выгрузка Анны одновременно с удалением аккаунта Игоря, у которых есть распавшаяся
    и текущая пары (удаление берёт текущую пару первой, затем все по id) — без 40P01."""
    keys = _keys()
    outcomes: list[object] = []
    for _ in range(24):
        anna, igor = await _person(engine, "Анна"), await _person(engine, "Игорь")
        await _couple(sessionmaker, anna, igor)
        _, old = await _send_encrypted(sessionmaker, keys, igor, "прежняя пара")
        await _deliver(engine, keys, old, "прежняя пара", adapted=False)
        await _call(sessionmaker, anna.user_id, "SELECT app_leave_couple()")
        await _couple(sessionmaker, igor, anna)
        _, cur = await _send_encrypted(sessionmaker, keys, igor, "текущая пара")
        await _deliver(engine, keys, cur, "текущая пара", adapted=True)

        async def export(uid: uuid.UUID = anna.user_id) -> object:
            try:
                return len(await _export(sessionmaker, uid))
            except DBAPIError as exc:
                return _sqlstate(exc)

        async def delete(uid: uuid.UUID = igor.user_id) -> object:
            try:
                return await _call(sessionmaker, uid, "SELECT app_delete_account()")
            except DBAPIError as exc:
                return _sqlstate(exc)

        got = await asyncio.gather(export(), delete())
        outcomes.extend(got)
        assert got[1] is None, got
        assert got[0] in (0, 2), got  # до удаления — оба сообщения, после — ни одного
    assert "40P01" not in outcomes


async def test_execute_only_for_app_role(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        for role, allowed in (
            ("feelings_app", True),
            ("feelings_api", False),
            ("feelings_worker", False),
            ("feelings_admin", False),
            ("public", False),
        ):
            row = await conn.execute(
                text(
                    "SELECT has_function_privilege(:r, 'app_export_partner_messages()', 'EXECUTE')"
                ),
                {"r": role},
            )
            assert row.scalar_one() is allowed, role
        definer, config, result = (
            await conn.execute(
                text(
                    "SELECT prosecdef, proconfig, pg_get_function_result(oid) FROM pg_proc "
                    "WHERE oid = 'app_export_partner_messages()'::regprocedure"
                )
            )
        ).one()
        assert definer is True and config == ["search_path=public, pg_temp"]
        assert "tone_score" not in result and "adapted_enc" not in result


async def test_app_still_cannot_read_original_directly(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """АР-11 не ослаблен: SELECT original_enc у приложения по-прежнему запрещён."""
    anna = await _person(engine, "Анна")
    async with engine.connect() as conn:
        assert (
            await conn.execute(
                text(
                    "SELECT has_column_privilege('feelings_app', 'partner_messages', "
                    "'original_enc', 'SELECT')"
                )
            )
        ).scalar_one() is False
    with pytest.raises(DBAPIError) as ei:
        await _call(sessionmaker, anna.user_id, "SELECT count(original_enc) FROM partner_messages")
    assert _sqlstate(ei.value) == "42501"


HEAD = "0015"  # последняя миграция (0015 — задача 3.5, раунд 1); после новой — обновить


async def _funcs_state(engine: AsyncEngine) -> tuple[object, ...]:
    """(есть app_export_partner_messages, есть app_export_safety_events, ревизия)."""
    async with engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT to_regprocedure('app_export_partner_messages()') IS NOT NULL, "
                "to_regprocedure('app_export_safety_events()') IS NOT NULL, "
                "(SELECT version_num FROM alembic_version)"
            )
        )
        return tuple(row.one())


async def _downgrade_and_back(
    engine: AsyncEngine, app_engine: AsyncEngine, target: str, expected: tuple[object, ...]
) -> None:
    try:
        assert await _funcs_state(engine) == (True, True, HEAD)
        alembic("downgrade", target)
        await engine.dispose()
        assert await _funcs_state(engine) == expected
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    assert await _funcs_state(engine) == (True, True, HEAD)
    async with engine.connect() as conn:
        for f in ("app_export_partner_messages()", "app_export_safety_events()"):
            assert (
                await conn.execute(
                    text("SELECT has_function_privilege('feelings_app', :f, 'EXECUTE')"),
                    {"f": f},
                )
            ).scalar_one() is True, f
    alembic("check")


async def test_migration_0006_reversible(engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    """head → 0005 → head: downgrade убирает обе функции выгрузки, upgrade возвращает их с
    правами; `alembic check` — модели и схема совпадают."""
    await _downgrade_and_back(engine, app_engine, "0005", (False, False, "0005"))


# --- 0007: app_export_safety_events() ------------------------------------------------------

SAFETY = "SELECT type::text AS type, created_at FROM app_export_safety_events()"


async def _safety_event(engine: AsyncEngine, uid: uuid.UUID, kind: str, ago_s: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO safety_events (user_id, type, created_at) "
                "VALUES (:u, CAST(:t AS safety_event_type), now() - make_interval(secs => :s))"
            ),
            {"u": uid, "t": kind, "s": ago_s},
        )


async def _safety(sm: SM, uid: uuid.UUID | None) -> list[tuple[str, object]]:
    async with user_scope(sm, uid) as s:
        return [(r.type, r.created_at) for r in await s.execute(text(SAFETY))]


async def test_safety_events_only_own_type_and_time(sessionmaker: SM, engine: AsyncEngine) -> None:
    """ТЗ §7, §9.3, АР-25 п.6: только свои события, только тип и время, по времени; партнёр и
    посторонний чужих не видят; без входа, неизвестный и помеченный удалённым — 42501."""
    anna, igor = await _person(engine, "Анна"), await _person(engine, "Игорь")
    olga = await _person(engine, "Ольга")
    await _couple(sessionmaker, anna, igor)
    await _safety_event(engine, anna.user_id, "self_harm", 10)
    await _safety_event(engine, anna.user_id, "violence", 60)
    await _safety_event(engine, igor.user_id, "threat_outgoing", 30)
    got = await _safety(sessionmaker, anna.user_id)
    assert [t for t, _ in got] == ["violence", "self_harm"]
    assert [t for t, _ in await _safety(sessionmaker, igor.user_id)] == ["threat_outgoing"]
    assert await _safety(sessionmaker, olga.user_id) == []
    for uid in (None, uuid.uuid4()):
        with pytest.raises(DBAPIError) as ei:
            await _safety(sessionmaker, uid)
        assert _sqlstate(ei.value) == "42501"
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": anna.user_id}
        )
    with pytest.raises(DBAPIError) as ei:
        await _safety(sessionmaker, anna.user_id)
    assert _sqlstate(ei.value) == "42501"


async def test_safety_events_table_stays_closed(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Чтение таблицы приложению не открыто: только INSERT, как в 0001."""
    anna = await _person(engine, "Анна")
    async with engine.connect() as conn:
        privs = (
            await conn.execute(
                text(
                    "SELECT has_table_privilege('feelings_app', 'safety_events', 'SELECT'), "
                    "has_table_privilege('feelings_app', 'safety_events', 'INSERT')"
                )
            )
        ).one()
    assert tuple(privs) == (False, True)
    with pytest.raises(DBAPIError) as ei:
        await _call(sessionmaker, anna.user_id, "SELECT count(*) FROM safety_events")
    assert _sqlstate(ei.value) == "42501"


async def test_safety_export_rights(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        for role, allowed in (
            ("feelings_app", True),
            ("feelings_api", False),
            ("feelings_worker", False),
            ("feelings_admin", False),
            ("public", False),
        ):
            row = await conn.execute(
                text("SELECT has_function_privilege(:r, 'app_export_safety_events()', 'EXECUTE')"),
                {"r": role},
            )
            assert row.scalar_one() is allowed, role
        definer, config, result = (
            await conn.execute(
                text(
                    "SELECT prosecdef, proconfig, pg_get_function_result(oid) FROM pg_proc "
                    "WHERE oid = 'app_export_safety_events()'::regprocedure"
                )
            )
        ).one()
        assert definer is True and config == ["search_path=public, pg_temp"]
        assert result == "TABLE(type safety_event_type, created_at timestamp with time zone)"


async def test_migration_0007_reversible(engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    """0007 → 0006 → head: downgrade убирает только app_export_safety_events; `alembic check`."""
    await _downgrade_and_back(engine, app_engine, "0006", (True, False, "0006"))

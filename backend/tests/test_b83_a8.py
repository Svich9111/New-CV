"""A8, раунд 1: Б-83 (миграция 0016, коммит 841776e; DECISIONS.md АР-48) и Б-84 (причина
задержки в ленте, коммит 4e477a4; АР-44, АР-45).

Б-83 (ТЗ §2 п.2 «ни одно сообщение не уходит без одобрения», §6.2 п.3–7, §10; ФТ-09):

- функций без аренды нет ни под каким именем и ни в какой схеме; ни одна роль (включая
  ``PUBLIC``) не получила при 0016 новых прав — набор исполнимых функций каждой роли на 0016 равен
  набору на 0015 за вычетом четырёх снятых; права на таблицы не изменились;
- 0016 → 0015 → 0016 не трогает данные сообщений; на 0015 прежние функции снова исполнимы
  воркером (downgrade рабочий, не только «определение совпало»), на 0016 — 42883; ``alembic check``
  чист, голова одна — текущая (``HEAD``);
- помощник тестов ``_pipeline_helpers`` не крадёт чужие строки: соседнее сообщение
  ``processing`` после доставки/остановки нужного — без аренды и с прежним счётчиком; сообщение
  под чужой арендой помощник не перехватывает;
- в коде приложения (кроме миграций) старых имён нет.

Б-84 (ТЗ §9.3, §4.5; АР-44, АР-45): причина и тип кризиса — только автору; на уровне функции
БД (под ролью приложения от имени получателя и постороннего) строки остановленного нет; вторая
граница (``couple_messages._withheld``) обнуляет поля не-автору и не-``withheld``; постраничная
лента получателя (страница = 1) ни на одной странице не показывает остановленное; выгрузка
данных получателя — ни id, ни текста, ни причины.

Данные выдуманные."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import sqlstate, user_scope
from app.repositories.couple_messages import _withheld
from tests import _pipeline_helpers as helpers
from tests import test_couple_messages_api as base
from tests.conftest import TEST_DATABASE_URL, alembic
from tests.test_account_export_db import HEAD
from tests.test_couple_functions import SM
from tests.test_couple_messages_api import FEED, Env, _pair, _person, feed, send
from tests.test_pipeline_db import _msg, _park, _send
from tests.test_pipeline_db import _pair as _db_pair

BACKEND = Path(__file__).resolve().parents[1]
env = base.env

REMOVED = (
    "pipeline_original(uuid)",
    "pipeline_deliver(uuid, bytea, boolean, smallint, boolean)",
    "pipeline_withhold(uuid)",
    "pipeline_withhold_leased(uuid, uuid, character varying, safety_event_type[])",
)
OLD_NAMES = ("pipeline_original", "pipeline_deliver", "pipeline_withhold")

# Исполнимые функции каждой роли (feelings_* и PUBLIC) — по всем пользовательским схемам.
_EXEC = """
SELECT r.rolname AS role, p.oid::regprocedure::text AS fn
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
CROSS JOIN (SELECT rolname FROM pg_roles WHERE rolname LIKE 'feelings%'
            UNION ALL SELECT 'public') r
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%'
  AND p.prokind = 'f'
  AND CASE WHEN r.rolname = 'public'
           THEN EXISTS (SELECT 1 FROM aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
                        WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE')
           ELSE has_function_privilege(r.rolname, p.oid, 'EXECUTE') END
"""
_TABLE_GRANTS = (
    "SELECT grantee, table_name, privilege_type FROM information_schema.role_table_grants "
    "WHERE table_schema = 'public' AND grantee LIKE 'feelings%' "
    "UNION ALL SELECT grantee, table_name || '.' || column_name, privilege_type "
    "FROM information_schema.column_privileges "
    "WHERE table_schema = 'public' AND grantee LIKE 'feelings%'"
)


async def _exec_sets(engine: AsyncEngine) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    async with engine.connect() as c:
        for row in await c.execute(text(_EXEC)):
            out.setdefault(row.role, set()).add(row.fn)
    return out


async def _table_grants(engine: AsyncEngine) -> set[tuple[str, str, str]]:
    async with engine.connect() as c:
        return {(str(r[0]), str(r[1]), str(r[2])) for r in await c.execute(text(_TABLE_GRANTS))}


async def _messages_digest(engine: AsyncEngine) -> tuple[int, str]:
    async with engine.connect() as c:
        row = (
            await c.execute(
                text(
                    "SELECT count(*), coalesce(md5(string_agg(t::text, '|' ORDER BY id)), '') "
                    "FROM partner_messages t"
                )
            )
        ).one()
    return int(row[0]), str(row[1])


async def _call_as_worker(engine: AsyncEngine, sql: str) -> str | None:
    """Вызов под ``feelings_worker`` в откатываемой транзакции → SQLSTATE ошибки или ``None``."""
    async with engine.connect() as c:
        tx = await c.begin()
        try:
            await c.execute(text("SET LOCAL ROLE feelings_worker"))
            await c.execute(text(sql))
            return None
        except DBAPIError as exc:
            return sqlstate(exc)
        finally:
            await tx.rollback()


OLD_CALLS = (
    "SELECT pipeline_original(gen_random_uuid())",
    "SELECT pipeline_withhold(gen_random_uuid())",
    "SELECT pipeline_deliver(gen_random_uuid(), '\\x00'::bytea, false, 0::smallint, false)",
    "SELECT pipeline_withhold_leased(gen_random_uuid(), gen_random_uuid(), 'safety'::varchar, "
    "NULL::safety_event_type[])",
)


# =========================================================================== Б-83: состояние 0016


async def test_no_unleased_function_under_any_name_or_schema(engine: AsyncEngine) -> None:
    """Б-83, АР-48: функций ``pipeline_original``/``pipeline_deliver``/``pipeline_withhold`` нет
    ни с какой сигнатурой и ни в какой схеме; ``pipeline_withhold_leased`` — одна, 5 аргументов;
    ни одна роль не может исполнить снятое (его просто нет)."""
    async with engine.connect() as c:
        names = (
            await c.execute(
                text(
                    "SELECT p.oid::regprocedure::text FROM pg_proc p "
                    "WHERE p.proname = ANY(:n) OR p.proname ILIKE 'pipeline_original%' "
                    "OR (p.proname = 'pipeline_withhold_leased' AND p.pronargs <> 5)"
                ),
                {"n": list(OLD_NAMES)},
            )
        ).scalars()
        assert list(names) == []
        for sig in REMOVED:
            assert (
                await c.execute(text("SELECT to_regprocedure(:f)"), {"f": sig})
            ).scalar() is None
    sets = await _exec_sets(engine)
    for role, fns in sets.items():
        assert not {f for f in fns if f.split("(")[0] in OLD_NAMES}, role
    for call in OLD_CALLS:
        assert await _call_as_worker(engine, call) == "42883", call


async def test_app_code_has_no_removed_names() -> None:
    """Код приложения (кроме миграций) снятые функции не вызывает; из ``texts`` убраны
    ``pipeline_read_original``/``pipeline_deliver``."""
    pattern = re.compile(r"\bpipeline_(original|deliver|withhold)\s*\(")
    hits = [
        str(p.relative_to(BACKEND))
        for p in (BACKEND / "app").rglob("*.py")
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert hits == []
    from app.repositories import texts

    assert not hasattr(texts, "pipeline_read_original") and not hasattr(texts, "pipeline_deliver")


async def test_0016_round_trip_rights_data_and_working_downgrade(
    sessionmaker: SM, engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """0016 → 0015 → 0016: (1) права — у каждой роли набор исполнимых функций на 0016 = на 0015
    минус четыре снятых (новых прав никому), права на таблицы равны; (2) данные сообщений
    побайтно те же; (3) на 0015 прежние функции исполнимы воркером, на 0016 — 42883;
    (4) ``alembic check`` чист, голова одна — текущая (``HEAD``)."""
    await _park(engine)
    anna, _igor, _ = await _db_pair(sessionmaker, engine)
    await _send(sessionmaker, engine, anna, b"a8-round-trip")
    head_exec, head_tables = await _exec_sets(engine), await _table_grants(engine)
    digest = await _messages_digest(engine)
    try:
        alembic("downgrade", "0015")
        await engine.dispose()
        old_exec, old_tables = await _exec_sets(engine), await _table_grants(engine)
        assert await _messages_digest(engine) == digest
        for call in OLD_CALLS:
            # Случайный id: функция есть и исполнима воркером — не 42883 (нет) и не 42501 (прав
            # нет); 4-аргументная обёртка на ``safety`` без событий отвечает 22023 — это её тело.
            assert await _call_as_worker(engine, call) not in ("42883", "42501"), call
        alembic("upgrade", "0016")
        await engine.dispose()
        # Сравнение 0015 ↔ 0016 — на самой 0016: после неё есть новые миграции (0017 — 4.4),
        # их права к Б-83 не относятся.
        new_exec, at16_tables = await _exec_sets(engine), await _table_grants(engine)
    finally:
        alembic("upgrade", "head")
        await engine.dispose()
        await app_engine.dispose()
    assert await _exec_sets(engine) == head_exec
    assert await _table_grants(engine) == head_tables
    assert at16_tables == old_tables
    assert await _messages_digest(engine) == digest
    removed_now = {r: old_exec.get(r, set()) - new_exec.get(r, set()) for r in old_exec}
    gained = {r: new_exec.get(r, set()) - old_exec.get(r, set()) for r in new_exec}
    assert all(not g for g in gained.values()), gained  # новых прав ни у одной роли
    worker_removed = {f.split("(")[0] for f in removed_now.get("feelings_worker", set())}
    assert worker_removed == {*OLD_NAMES, "pipeline_withhold_leased"}, removed_now
    assert len(removed_now["feelings_worker"]) == len(REMOVED)
    assert all(not v for r, v in removed_now.items() if r != "feelings_worker"), removed_now
    for call in OLD_CALLS:
        assert await _call_as_worker(engine, call) == "42883", call
    alembic("check")
    heads = subprocess.run(  # noqa: S603, ASYNC221 — фиксированная команда
        [sys.executable, "-m", "alembic", "-x", f"db_url={TEST_DATABASE_URL}", "heads"],
        cwd=BACKEND, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert heads.split() == [HEAD, "(head)"], heads


# =========================================================================== Б-83: помощник


async def _lease_row(engine: AsyncEngine, mid: uuid.UUID) -> tuple[Any, Any, int, str]:
    async with engine.connect() as c:
        r = (
            await c.execute(
                text(
                    "SELECT work_lease_token, work_lease_until, work_attempts, status::text "
                    "FROM partner_messages WHERE id = :m"
                ),
                {"m": mid},
            )
        ).one()
    return r[0], r[1], int(r[2]), str(r[3])


async def test_helper_does_not_steal_neighbour_processing_rows(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Б-83, АР-48: помощник захватывает только нужное сообщение — соседние ``processing``
    (другие тесты общей БД) после остановки и доставки нужного без аренды, счётчик тот же."""
    await _park(engine)
    anna, igor, _ = await _db_pair(sessionmaker, engine)
    target, _ = await _send(sessionmaker, engine, anna, b"a8-target")
    target2, _ = await _send(sessionmaker, engine, igor, b"a8-target-2")
    neighbours = [(await _send(sessionmaker, engine, who, b"a8-nb"))[0] for who in (anna, igor)]
    before = {m: await _lease_row(engine, m) for m in neighbours}
    assert all(b[0] is None and b[3] == "processing" for b in before.values())

    assert await helpers.withhold(engine, target) is True
    assert await helpers.deliver_blob(engine, target2, b"\x01a8") in ("delivered", "couple_ended")
    for m in neighbours:
        assert await _lease_row(engine, m) == before[m], "помощник захватил чужую строку"
    row = await _msg(engine, target)
    assert row is not None and row["status"] == "withheld" and row["work_lease_token"] is None
    assert row["work_lease_until"] is None


async def test_helper_does_not_take_over_foreign_lease(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Сообщение под чужой (живой) арендой: помощник его не перехватывает — завершение по
    случайному токену не проходит, аренда и статус прежние."""
    await _park(engine)
    anna, _igor, _ = await _db_pair(sessionmaker, engine)
    mid, _ = await _send(sessionmaker, engine, anna, b"a8-leased")
    first = await helpers.claim(engine, mid)
    assert first is not None
    held = await _lease_row(engine, mid)
    assert await helpers.withhold(engine, mid) is False
    assert await helpers.deliver_blob(engine, mid, b"\x01x") == "lost"
    assert await _lease_row(engine, mid) == held
    # Владелец аренды завершает своим токеном.
    assert await helpers.worker_call(engine, helpers.withhold_leased, mid, first.token) is True


# =========================================================================== Б-84


async def _trio(env: Env) -> tuple[Any, Any, Any]:
    anna, igor = await _person(env, "Анна", "Игорь"), await _person(env, "Игорь", "Анна")
    olga = await _person(env, "Ольга", "Пётр")
    await _pair(env, anna, igor)
    return anna, igor, olga


async def _stop_real(env: Env, mid: uuid.UUID, reason: str = "safety") -> None:
    """Остановка так, как её пишет воркер (захват → ``pipeline_withhold_leased``)."""
    assert await helpers.withhold(env.engine, mid, reason) is True  # type: ignore[arg-type]


_FEED_FN = (
    "SELECT id, outgoing, status::text AS status, withheld_reason, "
    "withheld_crisis::text AS withheld_crisis FROM app_couple_feed_page(100, NULL, NULL)"
)


async def test_feed_function_hides_withheld_from_recipient_and_outsider(env: Env) -> None:
    """Б-84, первая граница — функция БД: под ролью приложения от имени получателя и постороннего
    строки остановленного нет, и ни в одной строке нет причины/типа; автору — есть."""
    anna, igor, olga = await _trio(env)
    mid = await send(env, anna, env.mark("fn-stop"))
    await _stop_real(env, mid)
    for who, expect in ((anna, True), (igor, False), (olga, False)):
        async with user_scope(env.sm, who.id) as s:
            rows = (await s.execute(text(_FEED_FN))).all()
        ids = {uuid.UUID(str(r.id)) for r in rows}
        assert (mid in ids) is expect, who
        for r in rows:
            if not (r.outgoing and r.status == "withheld"):
                assert (r.withheld_reason, r.withheld_crisis) == (None, None), r
    async with user_scope(env.sm, anna.id) as s:
        mine = {uuid.UUID(str(r.id)): r for r in (await s.execute(text(_FEED_FN))).all()}
    assert (mine[mid].withheld_reason, mine[mid].withheld_crisis) == ("safety", "violence")


@pytest.mark.parametrize(
    ("outgoing", "status", "reason", "crisis", "expect"),
    [
        (False, "withheld", "safety", "violence", (None, None)),  # не автор
        (True, "delivered", "safety", "violence", (None, None)),  # не withheld
        (True, "processing", "pipeline_error", None, (None, None)),
        (True, "withheld", "model_refusal", "violence", ("model_refusal", None)),  # тип — только safety
        (True, "withheld", "safety", "self_harm", ("safety", "self_harm")),
    ],
)  # fmt: skip
def test_second_boundary_nulls_fields(
    outgoing: bool, status: str, reason: str, crisis: str | None, expect: tuple[Any, Any]
) -> None:
    """Б-84, вторая граница (``couple_messages._withheld``): даже если функция вернула бы поля,
    не-автору и не-``withheld`` они обнуляются; тип кризиса — только при ``safety``."""
    row = SimpleNamespace(
        outgoing=outgoing, status=status, withheld_reason=reason, withheld_crisis=crisis
    )
    got = _withheld(row)
    assert (got["withheld_reason"], got["withheld_crisis"]) == expect


async def test_recipient_paged_feed_never_shows_withheld(env: Env) -> None:
    """Постраничная лента получателя (страница = 1, курсор): ни на одной странице остановленного,
    поля причины везде ``null``; у автора — оно есть на своей странице."""
    anna, igor, _olga = await _trio(env)
    bodies = []
    for i in range(3):
        ok = await send(env, anna, env.mark(f"ok{i}"))
        await base.deliver(env, ok, env.mark(f"shown{i}"), adapted=False)
        stop_body = env.mark(f"stop{i}")
        bodies.append(stop_body)
        await _stop_real(env, await send(env, anna, stop_body), "pipeline_error")
    seen: list[dict[str, Any]] = []
    params: dict[str, Any] = {"limit": 1}
    for _ in range(20):
        page = await feed(env, igor, **params)
        seen += page["items"]
        if not page.get("next_cursor"):
            break
        params = {"limit": 1, "cursor": page["next_cursor"]}
    assert len(seen) == 3 and all(i["status"] != "withheld" for i in seen)
    assert all((i["withheld_reason"], i["withheld_crisis"]) == (None, None) for i in seen)
    dump = json.dumps(seen, ensure_ascii=False)
    assert not any(b in dump for b in bodies)
    mine = (await feed(env, anna, limit=50))["items"]
    assert sum(1 for i in mine if i["withheld_reason"] == "pipeline_error") == 3


async def test_recipient_export_has_no_withheld_message(env: Env) -> None:
    """ТЗ §6.3 (выгрузка), §9.3: выгрузка данных получателя не содержит остановленного
    сообщения — ни id, ни текста, ни причины; поля ``withheld`` нет у него вовсе."""
    anna, igor, _olga = await _trio(env)
    body = env.mark("export-stop")
    mid = await send(env, anna, body)
    await _stop_real(env, mid)
    resp = await env.client.get("/api/v1/me/export", headers=igor.headers)
    assert resp.status_code == 200, resp.text
    dump = resp.text
    assert str(mid) not in dump and body not in dump
    assert "withheld" not in dump and "violence" not in dump


async def test_recipient_cannot_reply_to_withheld(env: Env) -> None:
    """Подделанная ссылка ответа (ТЗ §4.4 п.5): получатель (и посторонняя) «отвечает» на
    остановленное — проверка цели ответа (``POST /drafts`` → ``reply_target_ok``) отказывает так
    же, как для несуществующего id: существование не раскрывается."""
    from app.repositories import texts

    anna, igor, olga = await _trio(env)
    mid = await send(env, anna, env.mark("reply-stop"))
    await _stop_real(env, mid)
    for who in (igor, olga, anna):
        assert await texts.reply_target_ok(env.sm, who.id, mid) is False
        assert await texts.reply_target_ok(env.sm, who.id, uuid.uuid4()) is False
    resp = await env.client.get(f"{FEED}/{mid}/original", headers=igor.headers)
    assert resp.status_code == 404

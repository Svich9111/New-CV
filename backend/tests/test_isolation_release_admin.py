"""4.3 (A8): набор выпуска — администратор без доступа к переписке (ТЗ §3, §9.1 п.5; BACKLOG Б-11;
АР-10, АР-24, АР-39; карточка 4.3 п.6).

Роль ``feelings_admin`` (``SET LOCAL ROLE`` у суперпользователя в откатываемой транзакции — роль
входа администратора в тестовом кластере не создаётся, Б-75; роли общие, не меняются): отказ на
``SELECT`` каждой личной таблицы §7 (включая права по столбцам) и на ``EXECUTE`` каждой
пользовательской (``app_*``) и рабочей (``pipeline_*``, ``memory_*``, ``kb_*``) функции. Ответы
``/admin/*`` не содержат текстов пользователей; токен администратора не открывает пользовательские
пути. Метрики ``/admin/metrics`` и ``/admin/prompts`` (контракт 2.8) ещё не реализованы — страж
маршрутов краснеет, как только они появятся, чтобы их проверили этим же набором."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import app
from tests._isolation_kit import mark, marks_in
from tests.test_admin_kb_api import DOCS, Env, book, env, upload  # noqa: F401 — фикстура env
from tests.test_chat_a8 import _conv, _turn, _user, at
from tests.test_isolation_release import PRIVATE_TABLES, SECURITY_DEFINER

pytestmark = pytest.mark.isolation


async def _as_admin(engine: AsyncEngine, sql: str) -> str | None:
    """Выполнить под ``feelings_admin`` и откатить; → текст ошибки или ``None`` (успех)."""
    async with engine.connect() as c:
        tx = await c.begin()
        try:
            await c.execute(text("SET LOCAL ROLE feelings_admin"))
            await c.execute(text(sql))
        except DBAPIError as exc:
            return str(exc.orig)
        finally:
            await tx.rollback()
    return None


@pytest.mark.parametrize("table", sorted(PRIVATE_TABLES))
async def test_admin_cannot_select_private_table(engine: AsyncEngine, table: str) -> None:
    """Б-11, §9.1 п.5: ``SELECT`` личной таблицы под ``feelings_admin`` — отказ; ни одного
    столбца с правом чтения (права по столбцам не обходят запрет)."""
    err = await _as_admin(engine, f"SELECT 1 FROM {table} LIMIT 1")
    assert err is not None and "permission denied" in err, (table, err)
    async with engine.connect() as c:
        cols = (await c.execute(text(
            "SELECT has_any_column_privilege('feelings_admin', :t, 'SELECT, INSERT, UPDATE, "
            "REFERENCES'), has_table_privilege('feelings_admin', :t, 'DELETE, TRUNCATE')"),
            {"t": table})).one()  # fmt: skip
    assert tuple(cols) == (False, False), table


USER_FUNCTIONS = sorted(f for f, role in SECURITY_DEFINER.items() if role != "feelings_admin")


@pytest.mark.parametrize("fn", USER_FUNCTIONS)
async def test_admin_cannot_execute_user_or_worker_functions(engine: AsyncEngine, fn: str) -> None:
    """Б-11, АР-11: каждая функция ``SECURITY DEFINER``, кроме ``admin_kb_*``, — отказ
    ``EXECUTE`` под ``feelings_admin`` (вызов с ``NULL``-аргументами: до тела не доходит)."""
    async with engine.connect() as c:
        nargs = (await c.execute(text(
            "SELECT pronargs FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname = :f"), {"f": fn})).scalar_one()  # fmt: skip
    args = ", ".join(["NULL"] * int(nargs))
    err = await _as_admin(engine, f"SELECT * FROM public.{fn}({args})")
    assert err is not None and "permission denied for function" in err, (fn, err)


def test_admin_routes_are_known() -> None:
    """Страж маршрутов ``/admin/*`` (карточка п.6): сейчас — только база знаний (2.6). Новый путь
    (``/admin/metrics``, ``/admin/prompts`` — 2.8) — добавить сюда и в проверку «без текстов»."""
    admin = {p for p in app.openapi()["paths"] if "/admin" in p}
    assert admin == {"/api/v1/admin/kb/documents", "/api/v1/admin/kb/documents/{document_id}"}


async def test_admin_api_has_no_user_texts_and_admin_token_opens_no_user_path(
    env: Env,  # noqa: F811
    engine: AsyncEngine,
) -> None:
    """§9.1 п.5: личные тексты пользователя (чат) есть в БД, но ни один ответ ``/admin/*`` их не
    содержит; токен администратора на пользовательских путях — 401 (не «чужая» сессия)."""
    secret = mark("ADMINLEAK")
    async with engine.begin() as c:
        uid, agent = await _user(c, name=f"Имя {secret}")
        conv = await _conv(c, agent)
        await _turn(c, conv, "owner", f"Исповедь {secret}", at(1))
    async with engine.connect() as c:
        blob = (await c.execute(text("SELECT text_enc FROM conversation_messages "
                                     "WHERE conversation_id = :c"), {"c": conv})).scalar_one()  # fmt: skip
    assert secret not in bytes(blob).decode("utf-8", "replace")  # шифротекст
    up = await upload(env, book("a8-iso"))
    assert up.status_code == 202, up.text
    seen = [up.text]
    for path in (DOCS, f"{DOCS}/{up.json()['id']}"):
        for method in ("GET", "DELETE"):
            r = await env.client.request(method, path, headers=env.admin)
            seen.append(f"{r.status_code} {r.text}")
    assert marks_in("\n".join(seen)) == set()
    for method, path in (("GET", "/api/v1/me"), ("GET", "/api/v1/me/export"),
                         ("GET", "/api/v1/chat/messages"), ("GET", "/api/v1/memory"),
                         ("GET", "/api/v1/couple/messages"), ("DELETE", "/api/v1/me")):  # fmt: skip
        r = await env.client.request(method, path, headers=env.admin)
        assert r.status_code == 401, (path, r.status_code)
        assert secret not in r.text
    async with engine.begin() as c:  # незавершённая сессия памяти не достаётся другим тестам
        await c.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})

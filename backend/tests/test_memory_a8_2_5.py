"""A8, задача 2.5 (раунд 1): сверка запроса клиента «Что агент помнит» с сервером.

ФТ-13; ТЗ §6.3 (удалённая запись не восстанавливается), §12 п.7 («удаление записи памяти
подтверждено проверкой в БД»). Клиент (`frontend/src/api/client.ts`, `memoryDelete`) шлёт ровно
`DELETE /api/v1/memory/{id}`: заголовки `Accept: application/json` и `Authorization: Bearer …`, без
тела, без `Content-Type`, без строки запроса; id — как пришёл в `GET /memory` (клиент принимает
uuid в любом регистре). Здесь этот запрос повторяется байт в байт и проверяется строка в БД.

Второе — вид полей, которые сервер отдаёт клиенту (строгая проверка `isMemoryItem` на клиенте):
`event_date` — `YYYY-MM-DD` в том числе для года < 100 (извлечение принимает такую дату от модели:
`date.fromisoformat`), `created_at` — дата-время ISO. Клиентская сторона — `frontend/src/api/
memory.a8.test.ts`. Данные выдуманные."""

from __future__ import annotations

import re
import uuid
from datetime import date

from sqlalchemy import text

from app.ai.memory import _date
from tests.test_couple_api import _create
from tests.test_memory_api import MEM, Env, _not_found, _person, add_item, env, items, mark

__all__ = ["env"]  # фикстура из test_memory_api


async def _row(env: Env, item: uuid.UUID) -> tuple[object, ...]:
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT text_enc, embedding, event_date, deleted_at, fingerprint "
                    "FROM memory_items WHERE id = :i"
                ),
                {"i": item},
            )
        ).one()
    return tuple(row)


async def test_client_shaped_delete_erases_row_in_db(env: Env) -> None:
    """Запрос в точности как у клиента → 204, пустое тело, `no-store`; в БД текст, вектор и
    дата стёрты, `deleted_at` и отпечаток заданы; повторный `GET` записи не возвращает, повтор
    того же запроса — 404 (повторное использование)."""
    anna = await _person(env.client)
    keep = await add_item(env, anna, "остаётся")
    gone = await add_item(
        env, anna, mark("client"), memory_type="date", event_date=date(2027, 6, 14)
    )
    # Как `Api.send`: только Accept и Authorization; тела и Content-Type нет.
    headers = {"Accept": "application/json", **anna.headers}
    req = env.client.build_request("DELETE", f"{MEM}/{gone}", headers=headers)
    assert req.content == b"" and "content-type" not in req.headers and req.url.query == b""
    resp = await env.client.send(req)
    assert resp.status_code == 204 and resp.content == b""
    assert resp.headers["cache-control"] == "no-store"
    text_enc, embedding, event_date, deleted_at, fingerprint = await _row(env, gone)
    assert text_enc is None and embedding is None and event_date is None
    assert deleted_at is not None and fingerprint is not None
    assert [i["id"] for i in await items(env, anna)] == [str(keep)]
    again = await env.client.send(
        env.client.build_request("DELETE", f"{MEM}/{gone}", headers=headers)
    )
    _not_found(again)


async def test_uppercase_id_from_client_is_the_same_record(env: Env) -> None:
    """Клиент принимает uuid в любом регистре и кладёт его в путь как есть (`encodeURIComponent`)
    — сервер удаляет ту же запись."""
    anna = await _person(env.client)
    gone = await add_item(env, anna, mark("upper"))
    resp = await env.client.delete(f"{MEM}/{str(gone).upper()}", headers=anna.headers)
    assert resp.status_code == 204
    assert (await _row(env, gone))[3] is not None
    assert await items(env, anna) == []


async def test_partner_cannot_delete_with_client_shaped_request(env: Env) -> None:
    """Чужой владелец (партнёр по паре) тем же запросом — 404, строка в БД не тронута."""
    anna = await _person(env.client)
    igor = await _person(env.client, "Игорь", "Анна")
    token = await _create(env.client, anna)  # type: ignore[arg-type]
    resp = await env.client.post(f"/api/v1/invites/{token}/accept", headers=igor.headers)
    assert resp.status_code == 200, resp.text
    item = await add_item(env, anna, mark("anna"))
    headers = {"Accept": "application/json", **igor.headers}
    _not_found(await env.client.delete(f"{MEM}/{item}", headers=headers))
    text_enc, _, _, deleted_at, _ = await _row(env, item)
    assert text_enc is not None and deleted_at is None


async def test_server_emits_event_date_with_year_below_100(env: Env) -> None:
    """Извлечение принимает от модели дату с годом < 100 (`date.fromisoformat`), сервер отдаёт её
    в `GET /memory` как `00YY-MM-DD` — это корректная `format: date` контракта. Клиент такую
    запись отвергает целиком (`isDay` через `Date.UTC`, годы 0–99 → 1900+), см. дефект в отчёте
    A8 и `frontend/src/api/memory.a8.test.ts`."""
    assert _date("0050-03-01") == date(50, 3, 1)
    anna = await _person(env.client)
    await add_item(env, anna, mark("old"), memory_type="date", event_date=date(50, 3, 1))
    await add_item(env, anna, mark("fresh"))
    out = await items(env, anna)
    assert sorted(str(i["event_date"]) for i in out) == ["0050-03-01", "None"]
    for i in out:
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})", i["created_at"]
        ), i["created_at"]

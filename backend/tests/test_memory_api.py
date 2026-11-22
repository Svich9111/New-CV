"""2.4, этап 2 (A2): «Что агент помнит» по HTTP — `GET /memory`, `DELETE /memory/{memory_id}`.

ФТ-13; ТЗ §2 п.6, §6.3; DECISIONS.md АР-17, АР-36; контракт `/memory`, `MemoryItem`. Записи
заводятся суперпользователем с шифротекстом приложения (как их пишет извлечение), читаются и
удаляются приложением под ролью входа `feelings_api` (RLS, `app_delete_memory`). Данные
выдуманные; тексты помечены уникальными метками — утечки ищутся поиском меток."""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.core import crypto
from app.core.crypto import Keyring, generate_key_entry, parse_keyring
from app.main import app
from tests.contract import assert_matches
from tests.test_auth_api import SECRET, TOKEN, _init_data, _tg
from tests.test_couple_api import BOT, _create

SM = async_sessionmaker[AsyncSession]
MEM = "/api/v1/memory"


@dataclass
class U:
    headers: dict[str, str]
    id: uuid.UUID


@dataclass
class Env:
    client: httpx.AsyncClient
    keys: Keyring
    engine: AsyncEngine


@pytest.fixture
async def env(sessionmaker: SM, engine: AsyncEngine) -> AsyncIterator[Env]:
    keys = parse_keyring(generate_key_entry("k1"), "k1")

    def settings() -> Settings:
        return Settings(
            telegram_bot_token=SecretStr(TOKEN),
            session_secret=SecretStr(SECRET),
            session_ttl_s=600,
            bot_username=BOT,
        )

    prev = (app.state.sessionmaker, app.state.keyring)
    app.state.sessionmaker, app.state.keyring = sessionmaker, keys
    app.dependency_overrides[get_settings] = settings
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield Env(c, keys, engine)
    app.dependency_overrides.clear()
    app.state.sessionmaker, app.state.keyring = prev


async def _login(c: httpx.AsyncClient) -> U:
    resp = await c.post("/api/v1/auth/telegram", json={"init_data": _init_data(_tg())})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return U({"Authorization": f"Bearer {body['access_token']}"}, uuid.UUID(body["me"]["id"]))


async def _person(c: httpx.AsyncClient, name: str = "Анна", partner: str = "Игорь") -> U:
    user = await _login(c)
    resp = await c.put(
        "/api/v1/me/onboarding",
        headers=user.headers,
        json={
            "name": name,
            "address_form": "на ты",
            "partner_name": partner,
            "timezone": "Europe/Moscow",
            "consent_terms": True,
            "consent_data": True,
            "consent_sensitive": True,
            "age_confirmed": True,
        },
    )
    assert resp.status_code == 200, resp.text
    return user


def mark(what: str) -> str:
    return f"метка-{what}-{uuid.uuid4().hex}"


async def add_item(
    env: Env,
    user: U,
    body: str,
    *,
    memory_type: str = "fact",
    source: str = "owner_dialog",
    event_date: date | None = None,
    confidence: float = 0.9,
    blob: bytes | None = None,
) -> uuid.UUID:
    """Запись памяти так, как её пишет извлечение: шифротекст `memory_items|text_enc|<id>`."""
    row_id = uuid.uuid4()
    async with env.engine.begin() as conn:
        agent = (
            await conn.execute(text("SELECT id FROM agents WHERE user_id = :u"), {"u": user.id})
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO memory_items (id, agent_id, type, source, text_enc, event_date, "
                "confidence, fingerprint) VALUES (:i, :a, CAST(:ty AS memory_type), "
                "CAST(:so AS memory_source), :t, :d, :c, :f)"
            ),
            {
                "i": row_id,
                "a": agent,
                "ty": memory_type,
                "so": source,
                "t": blob if blob is not None else env.keys.seal(crypto.MEMORY_TEXT, row_id, body),
                "d": event_date,
                "c": confidence,
                "f": secrets.token_bytes(32),
            },
        )
    return row_id


async def items(env: Env, user: U, **params: Any) -> list[dict[str, Any]]:
    resp = await env.client.get(MEM, headers=user.headers, params=params or None)
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    out: list[dict[str, Any]] = resp.json()["items"]
    for item in out:
        assert_matches(item, "MemoryItem")
    return out


def _not_found(resp: httpx.Response) -> None:
    assert resp.status_code == 404, resp.text
    assert_matches(resp.json(), "Error")
    assert resp.json()["code"] == "not_found"


# --------------------------------------------------------------------------- чтение


async def test_list_own_memory_by_category_with_source_date_confidence(env: Env) -> None:
    """Свои записи по категориям; наблюдения агента отделены (`observation`/`agent_inference`);
    тип, источник, дата события, уверенность, время; фильтр `?type`."""
    anna = await _person(env.client)
    fact = await add_item(env, anna, "Работает в библиотеке")
    day = await add_item(
        env, anna, "Годовщина", memory_type="date", event_date=date(2024, 6, 14), confidence=1.0
    )
    obs = await add_item(
        env,
        anna,
        "Похоже, ей важно, чтобы планы не менялись в последний момент",
        memory_type="observation",
        source="agent_inference",
        confidence=0.6,
    )
    got = await items(env, anna)
    assert [i["type"] for i in got] == ["fact", "date", "observation"]  # порядок перечисления
    by_id = {i["id"]: i for i in got}
    assert by_id[str(fact)]["text"] == "Работает в библиотеке"
    assert by_id[str(day)]["event_date"] == "2024-06-14"
    assert by_id[str(day)]["confidence"] == 1.0
    assert (by_id[str(obs)]["type"], by_id[str(obs)]["source"]) == (
        "observation",
        "agent_inference",
    )
    assert by_id[str(obs)]["confidence"] == pytest.approx(0.6)
    assert by_id[str(fact)]["event_date"] is None
    only = await items(env, anna, type="observation")
    assert [i["id"] for i in only] == [str(obs)]
    resp = await env.client.get(MEM, headers=anna.headers, params={"type": "nonsense"})
    assert resp.status_code == 422 and resp.json()["code"] == "validation_error"


async def test_newest_first_within_category(env: Env) -> None:
    anna = await _person(env.client)
    first = await add_item(env, anna, "первое")
    second = await add_item(env, anna, "второе")
    assert [i["id"] for i in await items(env, anna)] == [str(second), str(first)]


async def test_without_onboarding_and_without_session(env: Env) -> None:
    """Без онбординга — пустой список (агента нет), не 403: память под контролем всегда
    (АР-17); без сессии — 401."""
    fresh = await _login(env.client)
    assert await items(env, fresh) == []
    _not_found(await env.client.delete(f"{MEM}/{uuid.uuid4()}", headers=fresh.headers))
    for headers in ({}, {"Authorization": "Bearer nope"}):
        assert (await env.client.get(MEM, headers=headers)).status_code == 401
        resp = await env.client.delete(f"{MEM}/{uuid.uuid4()}", headers=headers)
        assert resp.status_code == 401


async def test_undecryptable_record_is_not_shown_and_list_does_not_fail(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    """Контракт: `MemoryItem.text` — только строка. Нерасшифрованная запись не показывается,
    список не падает; в журнале — код причины без id."""
    caplog.set_level(logging.DEBUG)
    anna = await _person(env.client)
    ok = await add_item(env, anna, "читается")
    bad = await add_item(env, anna, "", blob=b"\x00broken")
    got = await items(env, anna)
    assert [i["id"] for i in got] == [str(ok)]
    assert "memory_items.text_enc" in caplog.text and str(bad) not in caplog.text


# --------------------------------------------------------------------------- удаление


async def test_delete_then_not_listed_and_repeat_is_404(env: Env) -> None:
    """204 → записи нет в списке; текст, вектор и дата стёрты, отпечаток остался (АР-36);
    повтор — 404, несуществующий и неверный id — тот же 404."""
    anna = await _person(env.client)
    keep = await add_item(env, anna, "остаётся")
    gone = await add_item(
        env, anna, mark("delete"), memory_type="date", event_date=date(2025, 1, 1)
    )
    resp = await env.client.delete(f"{MEM}/{gone}", headers=anna.headers)
    assert resp.status_code == 204 and resp.content == b""
    assert [i["id"] for i in await items(env, anna)] == [str(keep)]
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT text_enc, embedding, event_date, deleted_at, fingerprint "
                    "FROM memory_items WHERE id = :i"
                ),
                {"i": gone},
            )
        ).one()
    assert row.text_enc is None and row.embedding is None and row.event_date is None
    assert row.deleted_at is not None and row.fingerprint is not None
    _not_found(await env.client.delete(f"{MEM}/{gone}", headers=anna.headers))
    _not_found(await env.client.delete(f"{MEM}/{uuid.uuid4()}", headers=anna.headers))
    _not_found(await env.client.delete(f"{MEM}/not-a-uuid", headers=anna.headers))


# --------------------------------------------------------------------------- изоляция


async def test_partner_and_outsider_neither_see_nor_delete(env: Env) -> None:
    """Партнёр по активной паре и посторонний не видят записи владельца и не удаляют их
    (404, неотличимо от несуществующей); запись остаётся у владельца."""
    anna = await _person(env.client)
    igor = await _person(env.client, "Игорь", "Анна")
    olga = await _person(env.client, "Ольга", "Пётр")
    token = await _create(env.client, anna)  # type: ignore[arg-type]
    resp = await env.client.post(f"/api/v1/invites/{token}/accept", headers=igor.headers)
    assert resp.status_code == 200, resp.text
    secret = mark("anna")
    item = await add_item(env, anna, secret)
    own_igor = await add_item(env, igor, mark("igor"))
    for other in (igor, olga):
        assert all(secret not in i["text"] for i in await items(env, other))
        _not_found(await env.client.delete(f"{MEM}/{item}", headers=other.headers))
    assert [i["id"] for i in await items(env, igor)] == [str(own_igor)]
    assert [i["text"] for i in await items(env, anna)] == [secret]


async def test_account_deletion_removes_memory_and_session(env: Env) -> None:
    anna = await _person(env.client)
    item = await add_item(env, anna, "что-то")
    assert (await env.client.delete("/api/v1/me", headers=anna.headers)).status_code == 204
    assert (await env.client.get(MEM, headers=anna.headers)).status_code == 401
    assert (await env.client.delete(f"{MEM}/{item}", headers=anna.headers)).status_code == 401
    async with env.engine.connect() as conn:
        n = (
            await conn.execute(text("SELECT count(*) FROM memory_items WHERE id = :i"), {"i": item})
        ).scalar_one()
    assert n == 0


# --------------------------------------------------------------------------- журналы


async def test_logs_have_no_texts_or_ids(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    anna = await _person(env.client)
    secret = mark("log")
    item = await add_item(env, anna, secret)
    await items(env, anna)
    assert (await env.client.delete(f"{MEM}/{item}", headers=anna.headers)).status_code == 204
    _not_found(await env.client.delete(f"{MEM}/{item}", headers=anna.headers))
    logged = "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)
    assert secret not in logged and secret.split("-")[-1] not in logged
    assert str(anna.id) not in logged

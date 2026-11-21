"""1.2 / ФТ-02: онбординг, три согласия, подтверждение 18+, зависимость OnboardedUser.

Критерии — docs/tasks/1.2.md; ТЗ §4.1 п.2, §9.1 п.2, §9.3. Данные выдуманные."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.deps import OnboardedUser
from app.config import get_settings
from app.errors import install_error_handlers
from app.main import app
from tests.contract import assert_matches
from tests.test_auth_api import _init_data, _settings, _tg

URL = "/api/v1/me/onboarding"
CONSENTS = ("consent_terms", "consent_data", "consent_sensitive")
MARKER = "Секрет-маркер-7f3a"

# Тестовый маршрут под OnboardedUser: подключается только здесь, в приложение не публикуется
# (test_contract проверяет, что сервер не выходит за контракт).
probe = FastAPI()
install_error_handlers(probe)


@probe.get("/probe")
async def _probe(user_id: OnboardedUser) -> dict[str, str]:
    return {"user_id": str(user_id)}


def _body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Анна",
        "address_form": "на ты",
        "partner_name": "Игорь",
        "timezone": "Europe/Moscow",
        "consent_terms": True,
        "consent_data": True,
        "consent_sensitive": True,
        "age_confirmed": True,
    }
    body.update(over)
    return body


@pytest.fixture
async def client(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.sessionmaker = sessionmaker
    app.dependency_overrides[get_settings] = _settings
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def probe_client(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    probe.state.sessionmaker = sessionmaker
    probe.dependency_overrides[get_settings] = _settings
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=probe), base_url="http://test"
    ) as c:
        yield c
    probe.dependency_overrides.clear()


async def _login(client: httpx.AsyncClient) -> tuple[dict[str, str], uuid.UUID]:
    resp = await client.post("/api/v1/auth/telegram", json={"init_data": _init_data(_tg())})
    assert resp.status_code == 200
    body = resp.json()
    return {"Authorization": f"Bearer {body['access_token']}"}, uuid.UUID(body["me"]["id"])


async def _user_row(engine: AsyncEngine, user_id: uuid.UUID) -> Any:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT name, address_form, partner_name, timezone, consent_terms_at, "
                    "consent_data_at, consent_sensitive_at, age_confirmed_at "
                    "FROM users WHERE id = :id"
                ),
                {"id": user_id},
            )
        ).one()


async def _count(engine: AsyncEngine, sql: str, user_id: uuid.UUID) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text(sql), {"id": user_id})).scalar_one())


AGENTS = "SELECT count(*) FROM agents WHERE user_id = :id"
UNDERAGE = "SELECT count(*) FROM safety_events WHERE user_id = :id AND type = 'underage'"


def _untouched(row: Any) -> None:
    assert row.name is None and row.address_form is None and row.partner_name is None
    assert row.consent_terms_at is None
    assert row.consent_data_at is None
    assert row.consent_sensitive_at is None
    assert row.age_confirmed_at is None


# --- Критерий 1: без всех трёх согласий — 422 ---------------------------------------------


@pytest.mark.parametrize("consent", CONSENTS)
@pytest.mark.parametrize("value", [False, None, "missing"])
async def test_missing_consent_is_422_and_nothing_saved(
    client: httpx.AsyncClient, engine: AsyncEngine, consent: str, value: object
) -> None:
    headers, uid = await _login(client)
    body = _body()
    if value == "missing":
        del body[consent]
    else:
        body[consent] = value
    resp = await client.put(URL, json=body, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"
    assert_matches(resp.json(), "Error")
    _untouched(await _user_row(engine, uid))
    assert await _count(engine, AGENTS, uid) == 0
    me = (await client.get("/api/v1/me", headers=headers)).json()
    assert me["onboarding_completed"] is False


# --- Критерий 2: 18+ не подтверждено — 403 underage ---------------------------------------


async def test_underage_is_403_and_logged_without_personal_data(
    client: httpx.AsyncClient,
    probe_client: httpx.AsyncClient,
    engine: AsyncEngine,
) -> None:
    headers, uid = await _login(client)
    resp = await client.put(URL, json=_body(age_confirmed=False, name=MARKER), headers=headers)
    assert resp.status_code == 403
    assert resp.json()["code"] == "underage"
    assert_matches(resp.json(), "Error")
    assert MARKER not in resp.text
    _untouched(await _user_row(engine, uid))
    assert await _count(engine, AGENTS, uid) == 0
    assert await _count(engine, UNDERAGE, uid) == 1
    me = (await client.get("/api/v1/me", headers=headers)).json()
    assert me["onboarding_completed"] is False
    denied = await probe_client.get("/probe", headers=headers)
    assert denied.status_code == 403
    assert denied.json()["code"] == "onboarding_required"


async def test_safety_events_hold_no_personal_data(engine: AsyncEngine) -> None:
    """Событие фиксируется без текста и личных данных (ТЗ §9.3): в таблице только служебные поля."""
    async with engine.connect() as conn:
        cols = (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'safety_events'"
                )
            )
        ).scalars()
        assert set(cols) == {"id", "user_id", "type", "created_at"}


async def test_underage_after_onboarding_revokes_access(
    client: httpx.AsyncClient, probe_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Заявленный возраст младше 18 закрывает доступ и уже прошедшему онбординг (ТЗ §9.3)."""
    headers, uid = await _login(client)
    assert (await client.put(URL, json=_body(), headers=headers)).status_code == 200
    assert (await probe_client.get("/probe", headers=headers)).status_code == 200
    resp = await client.put(URL, json=_body(age_confirmed=False), headers=headers)
    assert resp.status_code == 403
    assert resp.json()["code"] == "underage"
    assert (await _user_row(engine, uid)).age_confirmed_at is None
    assert await _count(engine, UNDERAGE, uid) == 1
    me = (await client.get("/api/v1/me", headers=headers)).json()
    assert me["onboarding_completed"] is False
    assert (await probe_client.get("/probe", headers=headers)).status_code == 403


async def test_age_confirmed_must_be_boolean(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    headers, uid = await _login(client)
    for bad in ("yes", "true", 1, None):
        resp = await client.put(URL, json=_body(age_confirmed=bad), headers=headers)
        assert resp.status_code == 422, bad
    body = _body()
    del body["age_confirmed"]
    assert (await client.put(URL, json=body, headers=headers)).status_code == 422
    assert await _count(engine, UNDERAGE, uid) == 0
    _untouched(await _user_row(engine, uid))


# --- Критерий 3: успешный онбординг и идемпотентность -------------------------------------


async def test_onboarding_saves_fields_consents_and_creates_one_agent(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    headers, uid = await _login(client)
    resp = await client.put(URL, json=_body(timezone="Asia/Vladivostok"), headers=headers)
    assert resp.status_code == 200
    me = resp.json()
    assert_matches(me, "Me")
    assert me["id"] == str(uid)
    assert me["onboarding_completed"] is True
    assert me["has_active_couple"] is False
    assert (me["name"], me["address_form"], me["partner_name"], me["timezone"]) == (
        "Анна",
        "на ты",
        "Игорь",
        "Asia/Vladivostok",
    )
    row = await _user_row(engine, uid)
    assert row.consent_terms_at is not None
    assert row.consent_data_at is not None
    assert row.consent_sensitive_at is not None
    assert row.age_confirmed_at is not None
    assert await _count(engine, AGENTS, uid) == 1
    assert await _count(engine, UNDERAGE, uid) == 0
    got = (await client.get("/api/v1/me", headers=headers)).json()
    assert got == me


async def test_repeat_onboarding_is_idempotent(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    headers, uid = await _login(client)
    assert (await client.put(URL, json=_body(), headers=headers)).status_code == 200
    first = await _user_row(engine, uid)
    resp = await client.put(
        URL, json=_body(name="Аня", partner_name="Игорёк", timezone="UTC"), headers=headers
    )
    assert resp.status_code == 200
    assert (resp.json()["name"], resp.json()["partner_name"], resp.json()["timezone"]) == (
        "Аня",
        "Игорёк",
        "UTC",
    )
    second = await _user_row(engine, uid)
    # Время первого согласия не переписывается повтором.
    assert second.consent_terms_at == first.consent_terms_at
    assert second.age_confirmed_at == first.age_confirmed_at
    assert await _count(engine, AGENTS, uid) == 1


async def test_parallel_onboarding_creates_single_agent(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    headers, uid = await _login(client)
    results = await asyncio.gather(
        *(client.put(URL, json=_body(), headers=headers) for _ in range(4))
    )
    assert [r.status_code for r in results] == [200] * 4
    assert await _count(engine, AGENTS, uid) == 1


async def test_timezone_defaults_to_moscow(client: httpx.AsyncClient) -> None:
    headers, _ = await _login(client)
    body = _body()
    del body["timezone"]
    resp = await client.put(URL, json=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["timezone"] == "Europe/Moscow"


async def test_foreign_ids_in_body_are_ignored(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Подставленный id чужого пользователя не меняет его данные: владелец — только из сессии."""
    headers_a, uid_a = await _login(client)
    _, uid_b = await _login(client)
    body = _body(id=str(uid_b), user_id=str(uid_b), name="Ольга")
    resp = await client.put(URL, json=body, headers=headers_a)
    assert resp.status_code == 200
    assert resp.json()["id"] == str(uid_a)
    _untouched(await _user_row(engine, uid_b))
    assert await _count(engine, AGENTS, uid_b) == 0


async def test_onboarding_requires_session(client: httpx.AsyncClient) -> None:
    assert (await client.put(URL, json=_body())).status_code == 401
    bad = await client.put(URL, json=_body(), headers={"Authorization": "Bearer garbage"})
    assert bad.status_code == 401
    assert bad.json()["code"] == "unauthorized"


async def test_deleted_user_cannot_onboard(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    headers, uid = await _login(client)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now() WHERE id = :id"), {"id": uid})
    assert (await client.put(URL, json=_body(), headers=headers)).status_code == 401
    assert await _count(engine, AGENTS, uid) == 0


# --- Критерий 4: OnboardedUser ------------------------------------------------------------


async def test_onboarded_user_dependency(
    client: httpx.AsyncClient, probe_client: httpx.AsyncClient
) -> None:
    """«Без согласий доступ к чату закрыт» (RoadMap 1.2) — на защищённом маршруте."""
    assert (await probe_client.get("/probe")).status_code == 401
    headers, uid = await _login(client)
    denied = await probe_client.get("/probe", headers=headers)
    assert denied.status_code == 403
    assert denied.json()["code"] == "onboarding_required"
    assert_matches(denied.json(), "Error")
    # Неудачная попытка без согласий доступ не открывает.
    await client.put(URL, json=_body(consent_sensitive=False), headers=headers)
    assert (await probe_client.get("/probe", headers=headers)).status_code == 403
    assert (await client.put(URL, json=_body(), headers=headers)).status_code == 200
    ok = await probe_client.get("/probe", headers=headers)
    assert ok.status_code == 200
    assert ok.json() == {"user_id": str(uid)}


async def test_onboarded_user_partial_consents_in_db_denied(
    client: httpx.AsyncClient, probe_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Если хоть одна отметка снята (например, отзыв согласия), доступ закрыт."""
    for column in (
        "consent_terms_at",
        "consent_data_at",
        "consent_sensitive_at",
        "age_confirmed_at",
    ):
        headers, uid = await _login(client)
        assert (await client.put(URL, json=_body(), headers=headers)).status_code == 200
        async with engine.begin() as conn:
            await conn.execute(
                text(f"UPDATE users SET {column} = NULL WHERE id = :id"), {"id": uid}
            )
        resp = await probe_client.get("/probe", headers=headers)
        assert resp.status_code == 403, column
        assert resp.json()["code"] == "onboarding_required"


async def test_onboarded_user_deleted_is_401(
    client: httpx.AsyncClient, probe_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    headers, uid = await _login(client)
    assert (await client.put(URL, json=_body(), headers=headers)).status_code == 200
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now() WHERE id = :id"), {"id": uid})
    assert (await probe_client.get("/probe", headers=headers)).status_code == 401


# --- Критерий 5: валидация полей ----------------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"name": ""},
        {"name": "   "},
        {"name": MARKER + "я" * 64},
        {"address_form": ""},
        {"address_form": MARKER + "я" * 64},
        {"partner_name": ""},
        {"partner_name": MARKER + "я" * 64},
        {"name": MARKER + "\x00"},
        {"partner_name": MARKER + "\nИгорь"},
        {"name": 42},
        {"name": None},
        {"timezone": "Mars/" + MARKER},
        {"timezone": "localtime"},
        {"timezone": "Europe/Moscow" + "x" * 60},
        {"timezone": ""},
        {"timezone": None},
        {"timezone": "../../etc/passwd"},
    ],
)
async def test_invalid_fields_are_422_without_echo(
    client: httpx.AsyncClient, engine: AsyncEngine, over: dict[str, Any]
) -> None:
    headers, uid = await _login(client)
    resp = await client.put(URL, json=_body(**over), headers=headers)
    assert resp.status_code == 422
    assert resp.json() == {"code": "validation_error", "message": "Проверьте данные"}
    assert MARKER not in resp.text
    _untouched(await _user_row(engine, uid))
    assert await _count(engine, AGENTS, uid) == 0


@pytest.mark.parametrize("field", ["name", "address_form", "partner_name"])
async def test_boundary_lengths_accepted(client: httpx.AsyncClient, field: str) -> None:
    headers, _ = await _login(client)
    for value in ("Я", "я" * 64):
        resp = await client.put(URL, json=_body(**{field: value}), headers=headers)
        assert resp.status_code == 200
        assert resp.json()[field] == value


@pytest.mark.parametrize("field", ["name", "address_form", "partner_name"])
async def test_missing_required_field_is_422(client: httpx.AsyncClient, field: str) -> None:
    headers, _ = await _login(client)
    body = _body()
    del body[field]
    assert (await client.put(URL, json=body, headers=headers)).status_code == 422


async def test_non_object_body_is_422(client: httpx.AsyncClient) -> None:
    headers, _ = await _login(client)
    for bad in ([], "x", 1):
        assert (await client.put(URL, json=bad, headers=headers)).status_code == 422
    resp = await client.put(
        URL, content=b"{not json", headers={**headers, "Content-Type": "application/json"}
    )
    assert resp.status_code == 422

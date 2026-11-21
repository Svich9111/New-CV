"""1.1 / ФТ-01: вход через Telegram по HTTP и защита эндпоинтов сессией."""

import json
import time
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.core.session import issue_token
from app.core.telegram import sign_init_data
from app.main import app

TOKEN = "123456:TEST-bot-token-not-real"
SECRET = "s" * 40


def _settings() -> Settings:
    return Settings(
        telegram_bot_token=SecretStr(TOKEN), session_secret=SecretStr(SECRET), session_ttl_s=600
    )


@pytest.fixture
async def client(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.sessionmaker = sessionmaker
    app.dependency_overrides[get_settings] = _settings
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _init_data(tg_id: int, token: str = TOKEN, auth_date: int | None = None) -> str:
    return sign_init_data(
        {
            "auth_date": str(auth_date or int(time.time())),
            "user": json.dumps({"id": tg_id, "first_name": "Игорь"}, ensure_ascii=False),
        },
        token,
    )


def _tg() -> int:
    return 5_000_000_000 + uuid.uuid4().int % 1_000_000_000


async def test_login_creates_user_and_returns_session(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/telegram", json={"init_data": _init_data(_tg())})
    assert resp.status_code == 200
    body = resp.json()
    assert body["expires_in"] == 600
    assert body["me"]["onboarding_completed"] is False
    assert body["me"]["has_active_couple"] is False
    me = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json()["id"] == body["me"]["id"]


async def test_repeat_login_same_user(client: httpx.AsyncClient) -> None:
    tg = _tg()
    first = (await client.post("/api/v1/auth/telegram", json={"init_data": _init_data(tg)})).json()
    second = (await client.post("/api/v1/auth/telegram", json={"init_data": _init_data(tg)})).json()
    assert first["me"]["id"] == second["me"]["id"]


async def test_forged_signature_rejected_and_no_user_created(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Поддельная подпись отклоняется (критерий RoadMap 1.1), пользователь не создаётся."""
    tg = _tg()
    resp = await client.post(
        "/api/v1/auth/telegram", json={"init_data": _init_data(tg, token="999:fake")}
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_init_data"
    async with engine.connect() as conn:
        count = await conn.execute(
            text("SELECT count(*) FROM users WHERE telegram_id = :t"), {"t": tg}
        )
        assert count.scalar_one() == 0


async def test_expired_init_data_rejected(client: httpx.AsyncClient) -> None:
    old = _init_data(_tg(), auth_date=int(time.time()) - 2 * 86400)
    resp = await client.post("/api/v1/auth/telegram", json={"init_data": old})
    assert resp.status_code == 401


async def test_me_requires_session(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/me")).status_code == 401
    bad = await client.get("/api/v1/me", headers={"Authorization": "Bearer garbage"})
    assert bad.status_code == 401
    assert bad.json()["code"] == "unauthorized"


async def test_token_signed_with_other_secret_rejected(client: httpx.AsyncClient) -> None:
    token = issue_token(uuid.uuid4(), "x" * 40, 600)
    resp = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


async def test_expired_session_rejected(client: httpx.AsyncClient) -> None:
    token = issue_token(uuid.uuid4(), SECRET, 60, now=time.time() - 3600)
    resp = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


async def test_token_for_unknown_user_rejected(client: httpx.AsyncClient) -> None:
    token = issue_token(uuid.uuid4(), SECRET, 600)
    resp = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


async def _count(engine: AsyncEngine, tg: int) -> int:
    async with engine.connect() as conn:
        res = await conn.execute(
            text("SELECT count(*) FROM users WHERE telegram_id = :t"), {"t": tg}
        )
        return int(res.scalar_one())


async def test_user_created_once_without_telegram_name(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Собирается минимум (ТЗ §9.1 п.2): только telegram_id; имя спрашивается в онбординге."""
    tg = _tg()
    for _ in range(2):
        assert (
            await client.post("/api/v1/auth/telegram", json={"init_data": _init_data(tg)})
        ).status_code == 200
    assert await _count(engine, tg) == 1
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT name, address_form, partner_name FROM users WHERE telegram_id = :t"),
                {"t": tg},
            )
        ).one()
    assert tuple(row) == (None, None, None)


async def test_future_auth_date_rejected_http(client: httpx.AsyncClient) -> None:
    future = _init_data(_tg(), auth_date=int(time.time()) + 3600)
    assert (
        await client.post("/api/v1/auth/telegram", json={"init_data": future})
    ).status_code == 401


async def test_non_ascii_hash_is_401_not_500(client: httpx.AsyncClient) -> None:
    base = _init_data(_tg()).rsplit("&hash=", 1)[0]
    resp = await client.post("/api/v1/auth/telegram", json={"init_data": base + "&hash=%D0%B0"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_init_data"


@pytest.mark.parametrize(
    "settings",
    [
        Settings(telegram_bot_token=SecretStr(""), session_secret=SecretStr(SECRET)),
        Settings(telegram_bot_token=SecretStr(TOKEN), session_secret=SecretStr("")),
        Settings(telegram_bot_token=SecretStr(TOKEN), session_secret=SecretStr("short")),
    ],
)
async def test_misconfigured_service_refuses_before_creating_user(
    client: httpx.AsyncClient, engine: AsyncEngine, settings: Settings
) -> None:
    app.dependency_overrides[get_settings] = lambda: settings
    tg = _tg()
    resp = await client.post("/api/v1/auth/telegram", json={"init_data": _init_data(tg)})
    assert resp.status_code == 503
    assert resp.json()["code"] == "service_unavailable"
    assert await _count(engine, tg) == 0


def test_startup_fails_without_keys() -> None:
    from app.config import ConfigError, check_auth_config

    with pytest.raises(ConfigError):
        check_auth_config(Settings(telegram_bot_token=SecretStr(""), session_secret=SecretStr("")))


async def test_deleted_user_loses_access_immediately(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Удалённый аккаунт теряет доступ сразу, а не по истечении токена (ФТ-21)."""
    tg = _tg()
    body = (await client.post("/api/v1/auth/telegram", json={"init_data": _init_data(tg)})).json()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET deleted_at = now() WHERE telegram_id = :t"), {"t": tg}
        )
    resp = await client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert resp.status_code == 401


async def test_responses_match_contract(client: httpx.AsyncClient) -> None:
    """Ответы — по схемам api/openapi.yaml."""
    from tests.contract import assert_matches

    ok = await client.post("/api/v1/auth/telegram", json={"init_data": _init_data(_tg())})
    assert_matches(ok.json(), "Session")
    me = await client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {ok.json()['access_token']}"}
    )
    assert_matches(me.json(), "Me")
    bad = await client.post("/api/v1/auth/telegram", json={"init_data": "x=1"})
    assert_matches(bad.json(), "Error")


async def test_validation_error_does_not_echo_body(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/telegram", json={"init_data": ""})
    assert resp.status_code == 422
    assert resp.json() == {"code": "validation_error", "message": "Проверьте данные"}

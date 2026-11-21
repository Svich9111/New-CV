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


async def test_validation_error_does_not_echo_body(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/telegram", json={"init_data": ""})
    assert resp.status_code == 422
    assert resp.json() == {"code": "validation_error", "message": "Проверьте данные"}

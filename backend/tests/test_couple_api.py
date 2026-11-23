"""1.3 / ФТ-03, ФТ-04: пары и приглашения по HTTP.

Критерии — docs/tasks/1.3.md; ТЗ §4.1 п.3–6, §4.5 п.2, §8, §9.3. Данные выдуманные."""

import base64
import hashlib
import logging
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.core.invites import hash_token, invite_link, new_token
from app.core.logging import RedactInviteTokens
from app.main import app
from tests.contract import assert_matches
from tests.test_auth_api import SECRET, TOKEN, _init_data, _tg

BOT = "FeelingsTestBot"
LINK_RE = re.compile(rf"^https://t\.me/{BOT}\?startapp=([A-Za-z0-9_-]{{43}})$")


def _settings() -> Settings:
    return Settings(
        telegram_bot_token=SecretStr(TOKEN),
        session_secret=SecretStr(SECRET),
        session_ttl_s=600,
        bot_username=BOT,
    )


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


class User:
    def __init__(self, headers: dict[str, str], user_id: uuid.UUID) -> None:
        self.headers, self.id = headers, user_id


async def _login(client: httpx.AsyncClient) -> User:
    resp = await client.post("/api/v1/auth/telegram", json={"init_data": _init_data(_tg())})
    assert resp.status_code == 200
    body = resp.json()
    return User({"Authorization": f"Bearer {body['access_token']}"}, uuid.UUID(body["me"]["id"]))


async def _onboard(client: httpx.AsyncClient, user: User, name: str) -> None:
    resp = await client.put(
        "/api/v1/me/onboarding",
        headers=user.headers,
        json={
            "name": name,
            "address_form": "на ты",
            "partner_name": "Партнёр",
            "timezone": "Europe/Moscow",
            "consent_terms": True,
            "consent_data": True,
            "consent_sensitive": True,
            "age_confirmed": True,
        },
    )
    assert resp.status_code == 200, resp.text


async def _person(client: httpx.AsyncClient, name: str) -> User:
    user = await _login(client)
    await _onboard(client, user, name)
    return user


def _token(resp: httpx.Response) -> str:
    match = LINK_RE.match(resp.json()["link"])
    assert match, resp.json()["link"]
    return match.group(1)


async def _create(client: httpx.AsyncClient, user: User) -> str:
    resp = await client.post("/api/v1/couple", headers=user.headers)
    assert resp.status_code == 201, resp.text
    assert_matches(resp.json(), "InviteCreated")
    return _token(resp)


def _error(resp: httpx.Response, status: int, code: str | None = None) -> None:
    assert resp.status_code == status, resp.text
    assert_matches(resp.json(), "Error")
    if code is None and status == 404:
        code = "not_found"  # единый 404 (критерий 3): всегда один и тот же код
    assert code is not None, "код ошибки проверяется всегда (ревью 1.3 MINOR-5)"
    assert resp.json()["code"] == code, resp.json()


async def _active_pair(client: httpx.AsyncClient) -> tuple[User, User, str]:
    anna, igor = await _person(client, "Анна"), await _person(client, "Игорь")
    token = await _create(client, anna)
    resp = await client.post(f"/api/v1/invites/{token}/accept", headers=igor.headers)
    assert resp.status_code == 200, resp.text
    return anna, igor, token


# --- Полный сценарий ---------------------------------------------------------------------


async def test_full_scenario_anna_and_igor(client: httpx.AsyncClient) -> None:
    """Анна создаёт пару → ссылка → Игорь (после онбординга) смотрит и принимает → оба видят
    активную пару → выход → оба могут создать новую."""
    anna = await _person(client, "Анна")

    created = await client.post("/api/v1/couple", headers=anna.headers)
    assert created.status_code == 201
    assert_matches(created.json(), "InviteCreated")
    token = _token(created)
    couple_id = created.json()["couple_id"]
    expires = datetime.fromisoformat(created.json()["expires_at"])
    assert timedelta(days=6, hours=23) < expires - datetime.now(UTC) <= timedelta(days=7)

    pending = await client.get("/api/v1/couple", headers=anna.headers)
    assert pending.status_code == 200
    assert_matches(pending.json(), "Couple")
    assert pending.json() == {
        "id": couple_id,
        "status": "pending",
        "members": [{"role": "initiator", "name": "Анна", "is_me": True}],
    }

    # Игорь вошёл, но ещё не прошёл онбординг: приглашение видно, принять нельзя.
    igor = await _login(client)
    view = await client.get(f"/api/v1/invites/{token}", headers=igor.headers)
    assert view.status_code == 200
    assert view.json() == {"inviter_name": "Анна", "expires_at": created.json()["expires_at"]}
    early = await client.post(f"/api/v1/invites/{token}/accept", headers=igor.headers)
    _error(early, 403, "onboarding_required")

    await _onboard(client, igor, "Игорь")
    accepted = await client.post(f"/api/v1/invites/{token}/accept", headers=igor.headers)
    assert accepted.status_code == 200
    assert_matches(accepted.json(), "Couple")
    assert accepted.json()["id"] == couple_id
    assert accepted.json()["status"] == "active"

    for me in (anna, igor):
        resp = await client.get("/api/v1/couple", headers=me.headers)
        assert resp.status_code == 200
        assert_matches(resp.json(), "Couple")
        body = resp.json()
        assert body["id"] == couple_id and body["status"] == "active"
        members = {m["name"]: m for m in body["members"]}
        assert members == {
            "Анна": {"role": "initiator", "name": "Анна", "is_me": me is anna},
            "Игорь": {"role": "partner", "name": "Игорь", "is_me": me is igor},
        }
        profile = (await client.get("/api/v1/me", headers=me.headers)).json()
        assert profile["has_active_couple"] is True

    left = await client.post("/api/v1/couple/leave", headers=igor.headers)
    assert left.status_code == 204
    assert left.content == b""

    for me in (anna, igor):
        _error(await client.get("/api/v1/couple", headers=me.headers), 404)
        profile = (await client.get("/api/v1/me", headers=me.headers)).json()
        assert profile["has_active_couple"] is False
    for me in (anna, igor):
        new_token_ = await _create(client, me)
        assert new_token_ != token


# --- RoadMap 1.3: повторное использование, истёкший срок, вторая пара --------------------


async def test_reused_link_is_gone(client: httpx.AsyncClient) -> None:
    _, igor, token = await _active_pair(client)
    olga = await _person(client, "Ольга")
    _error(await client.get(f"/api/v1/invites/{token}", headers=olga.headers), 410, "invite_used")
    resp = await client.post(f"/api/v1/invites/{token}/accept", headers=olga.headers)
    _error(resp, 410, "invite_used")
    again = await client.post(f"/api/v1/invites/{token}/accept", headers=igor.headers)
    _error(again, 410, "invite_used")
    # Ольга в пару не попала.
    _error(await client.get("/api/v1/couple", headers=olga.headers), 404)


async def test_expired_link_is_gone(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    anna, igor = await _person(client, "Анна"), await _person(client, "Игорь")
    token = await _create(client, anna)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE invites SET expires_at = now() - interval '1 second' WHERE token_hash = :h"
            ),
            {"h": hashlib.sha256(token.encode()).digest()},
        )
    resp = await client.get(f"/api/v1/invites/{token}", headers=igor.headers)
    _error(resp, 410, "invite_expired")
    resp = await client.post(f"/api/v1/invites/{token}/accept", headers=igor.headers)
    _error(resp, 410, "invite_expired")
    # Новая ссылка для той же ожидающей пары работает.
    reissued = await client.post("/api/v1/couple/invite", headers=anna.headers)
    assert reissued.status_code == 201
    resp = await client.post(f"/api/v1/invites/{_token(reissued)}/accept", headers=igor.headers)
    assert resp.status_code == 200


async def test_second_couple_conflicts(client: httpx.AsyncClient) -> None:
    anna, igor, _ = await _active_pair(client)
    # Уже в активной паре — вторую не создать (ФТ-04).
    for user in (anna, igor):
        _error(await client.post("/api/v1/couple", headers=user.headers), 409, "already_in_couple")
    # Ожидающая пара тоже считается: создать вторую нельзя.
    olga = await _person(client, "Ольга")
    await _create(client, olga)
    _error(await client.post("/api/v1/couple", headers=olga.headers), 409, "already_in_couple")
    # Ольга (в своей ожидающей паре) не может принять чужое приглашение.
    pavel = await _person(client, "Павел")
    pavel_token = await _create(client, pavel)
    resp = await client.post(f"/api/v1/invites/{pavel_token}/accept", headers=olga.headers)
    _error(resp, 409, "already_in_couple")
    # Участник активной пары тоже.
    resp = await client.post(f"/api/v1/invites/{pavel_token}/accept", headers=igor.headers)
    _error(resp, 409, "already_in_couple")


async def test_own_invite_cannot_be_accepted(client: httpx.AsyncClient) -> None:
    anna = await _person(client, "Анна")
    token = await _create(client, anna)
    resp = await client.post(f"/api/v1/invites/{token}/accept", headers=anna.headers)
    _error(resp, 409, "own_invite")
    couple = (await client.get("/api/v1/couple", headers=anna.headers)).json()
    assert couple["status"] == "pending" and len(couple["members"]) == 1


# --- Перевыпуск ссылки -------------------------------------------------------------------


async def test_reissue_revokes_previous_link(client: httpx.AsyncClient) -> None:
    anna, igor = await _person(client, "Анна"), await _person(client, "Игорь")
    created = await client.post("/api/v1/couple", headers=anna.headers)
    old = _token(created)
    reissued = await client.post("/api/v1/couple/invite", headers=anna.headers)
    assert reissued.status_code == 201
    assert_matches(reissued.json(), "InviteCreated")
    assert reissued.json()["couple_id"] == created.json()["couple_id"]
    new = _token(reissued)
    assert new != old
    _error(await client.get(f"/api/v1/invites/{old}", headers=igor.headers), 410, "invite_used")
    resp = await client.post(f"/api/v1/invites/{old}/accept", headers=igor.headers)
    _error(resp, 410, "invite_used")
    resp = await client.post(f"/api/v1/invites/{new}/accept", headers=igor.headers)
    assert resp.status_code == 200


async def test_reissue_without_couple_is_404(client: httpx.AsyncClient) -> None:
    olga = await _person(client, "Ольга")
    _error(await client.post("/api/v1/couple/invite", headers=olga.headers), 404)


async def test_reissue_for_active_couple_conflicts(client: httpx.AsyncClient) -> None:
    anna, igor, _ = await _active_pair(client)
    for user in (anna, igor):
        resp = await client.post("/api/v1/couple/invite", headers=user.headers)
        _error(resp, 409, "couple_not_pending")


# --- Выход -------------------------------------------------------------------------------


async def test_leave_by_initiator_dissolves_for_both(client: httpx.AsyncClient) -> None:
    anna, igor, _ = await _active_pair(client)
    assert (await client.post("/api/v1/couple/leave", headers=anna.headers)).status_code == 204
    for user in (anna, igor):
        _error(await client.get("/api/v1/couple", headers=user.headers), 404)
        _error(await client.post("/api/v1/couple/leave", headers=user.headers), 404)


async def test_leave_pending_couple_revokes_link(client: httpx.AsyncClient) -> None:
    anna, igor = await _person(client, "Анна"), await _person(client, "Игорь")
    token = await _create(client, anna)
    assert (await client.post("/api/v1/couple/leave", headers=anna.headers)).status_code == 204
    _error(await client.get(f"/api/v1/invites/{token}", headers=igor.headers), 410, "invite_used")
    resp = await client.post(f"/api/v1/invites/{token}/accept", headers=igor.headers)
    _error(resp, 410, "invite_used")


async def test_leave_without_couple_is_404(client: httpx.AsyncClient) -> None:
    olga = await _person(client, "Ольга")
    _error(await client.post("/api/v1/couple/leave", headers=olga.headers), 404)


async def test_leave_writes_audit_without_texts(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    anna, igor, _ = await _active_pair(client)
    couple_id = (await client.get("/api/v1/couple", headers=anna.headers)).json()["id"]
    assert (await client.post("/api/v1/couple/leave", headers=igor.headers)).status_code == 204
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT actor_id, action, object_type FROM audit_log WHERE object_id = :c"),
                {"c": couple_id},
            )
        ).all()
    assert [(r.actor_id, r.action, r.object_type) for r in rows] == [
        (igor.id, "couple.leave", "couple")
    ]


# --- Доступ: сессия, онбординг, посторонний ----------------------------------------------

# Требуют онбординга. Выход (АР-17) и просмотр приглашения — только входа.
ONBOARDED_ENDPOINTS = [
    ("get", "/api/v1/couple"),
    ("post", "/api/v1/couple"),
    ("post", "/api/v1/couple/invite"),
    ("post", "/api/v1/invites/{token}/accept"),
]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        *ONBOARDED_ENDPOINTS,
        ("post", "/api/v1/couple/leave"),
        ("get", "/api/v1/invites/{token}"),
    ],
)
async def test_requires_session(client: httpx.AsyncClient, method: str, path: str) -> None:
    url = path.format(token=new_token())
    _error(await client.request(method, url), 401, "unauthorized")
    resp = await client.request(method, url, headers={"Authorization": "Bearer forged"})
    _error(resp, 401, "unauthorized")


@pytest.mark.parametrize(("method", "path"), ONBOARDED_ENDPOINTS)
async def test_requires_onboarding(
    client: httpx.AsyncClient, engine: AsyncEngine, method: str, path: str
) -> None:
    anna = await _person(client, "Анна")
    token = await _create(client, anna)
    newcomer = await _login(client)
    resp = await client.request(method, path.format(token=token), headers=newcomer.headers)
    _error(resp, 403, "onboarding_required")
    # Ничего не изменилось: у новичка нет пары, приглашение Анны действует.
    async with engine.connect() as conn:
        members = await conn.execute(
            text("SELECT count(*) FROM couple_members WHERE user_id = :u"), {"u": newcomer.id}
        )
        assert members.scalar_one() == 0
        status = await conn.execute(
            text("SELECT status::text FROM invites WHERE token_hash = :h"),
            {"h": hash_token(token)},
        )
        assert status.scalar_one() == "active"


async def test_age_revoked_member_cannot_accept(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Отзыв 18+ после онбординга (ТЗ §9.3): принять приглашение нельзя."""
    anna, igor = await _person(client, "Анна"), await _person(client, "Игорь")
    token = await _create(client, anna)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET age_confirmed_at = NULL WHERE id = :u"), {"u": igor.id}
        )
    resp = await client.post(f"/api/v1/invites/{token}/accept", headers=igor.headers)
    _error(resp, 403, "onboarding_required")


async def test_view_invite_before_onboarding(client: httpx.AsyncClient) -> None:
    anna = await _person(client, "Анна")
    token = await _create(client, anna)
    newcomer = await _login(client)
    resp = await client.get(f"/api/v1/invites/{token}", headers=newcomer.headers)
    assert resp.status_code == 200
    assert set(resp.json()) == {"inviter_name", "expires_at"}
    assert resp.json()["inviter_name"] == "Анна"


async def test_stranger_does_not_see_couple(client: httpx.AsyncClient) -> None:
    anna, igor, _ = await _active_pair(client)
    olga = await _person(client, "Ольга")
    resp = await client.get("/api/v1/couple", headers=olga.headers)
    _error(resp, 404)
    for word in ("Анна", "Игорь", str(anna.id), str(igor.id)):
        assert word not in resp.text


async def test_couple_members_only_own_couple(client: httpx.AsyncClient) -> None:
    """Две пары: каждый видит только свою."""
    anna, igor, _ = await _active_pair(client)
    olga, pavel = await _person(client, "Ольга"), await _person(client, "Павел")
    token = await _create(client, olga)
    assert (
        await client.post(f"/api/v1/invites/{token}/accept", headers=pavel.headers)
    ).status_code == 200
    for user, names in (
        (anna, {"Анна", "Игорь"}),
        (igor, {"Анна", "Игорь"}),
        (olga, {"Ольга", "Павел"}),
        (pavel, {"Ольга", "Павел"}),
    ):
        body = (await client.get("/api/v1/couple", headers=user.headers)).json()
        assert {m["name"] for m in body["members"]} == names
        assert sum(m["is_me"] for m in body["members"]) == 1


# --- Токен: не найден, поддельный, формат не раскрывается --------------------------------


def _not_found_bodies() -> list[str]:
    return [
        new_token(),  # правильный формат, но такого нет
        "A" * 43,
        "a" * 32,
        "b" * 64,
        "short",
        "x" * 65,
        "bad*chars" + "a" * 30,
        "%20" * 15,
    ]


@pytest.mark.parametrize("token", _not_found_bodies())
async def test_unknown_or_malformed_token_is_404(client: httpx.AsyncClient, token: str) -> None:
    igor = await _person(client, "Игорь")
    view = await client.get(f"/api/v1/invites/{token}", headers=igor.headers)
    accept = await client.post(f"/api/v1/invites/{token}/accept", headers=igor.headers)
    _error(view, 404)
    _error(accept, 404)
    # 404 одинаков для всех случаев: по ответу нельзя понять, в чём причина.
    assert view.json() == accept.json() == {"code": "not_found", "message": view.json()["message"]}


async def test_hash_of_token_as_token_is_404(client: httpx.AsyncClient) -> None:
    """Знание хеша из БД не даёт войти: хеш (base64url) — не токен."""
    anna, igor = await _person(client, "Анна"), await _person(client, "Игорь")
    token = await _create(client, anna)
    fake = base64.urlsafe_b64encode(hash_token(token)).rstrip(b"=").decode()
    _error(await client.get(f"/api/v1/invites/{fake}", headers=igor.headers), 404)
    _error(await client.post(f"/api/v1/invites/{fake}/accept", headers=igor.headers), 404)


# --- Токен: хранение и журналы -----------------------------------------------------------


async def test_only_token_hash_is_stored(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    anna = await _person(client, "Анна")
    token = await _create(client, anna)
    raw = base64.urlsafe_b64decode(token + "=")
    assert len(raw) == 32
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT i.token_hash, row_to_json(i)::text AS dump FROM invites i "
                    "JOIN couple_members cm ON cm.couple_id = i.couple_id WHERE cm.user_id = :u"
                ),
                {"u": anna.id},
            )
        ).all()
    assert len(rows) == 1
    assert bytes(rows[0].token_hash) == hashlib.sha256(token.encode()).digest()
    assert token not in rows[0].dump
    assert raw.hex() not in rows[0].dump


async def test_token_not_in_logs(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    anna, igor = await _person(client, "Анна"), await _person(client, "Игорь")
    created = await client.post("/api/v1/couple", headers=anna.headers)
    token = _token(created)
    await client.get(f"/api/v1/invites/{token}", headers=igor.headers)
    await client.post(f"/api/v1/invites/{token}/accept", headers=igor.headers)
    await client.post(f"/api/v1/invites/{token}/accept", headers=igor.headers)
    # Журнал тестового клиента httpx — сторона клиента, не сервера.
    server = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    for record in server:
        assert token not in record.getMessage(), record.name


def test_access_log_redacts_invite_token() -> None:
    """uvicorn пишет путь запроса в журнал доступа — токен из пути вырезается."""
    token = new_token()
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "POST", f"/api/v1/invites/{token}/accept?x=1", "1.1", 200),
        None,
    )
    assert RedactInviteTokens().filter(record) is True
    message = record.getMessage()
    assert token not in message
    assert "/api/v1/invites/***/accept?x=1" in message
    assert logging.getLogger("uvicorn.access").filters, "фильтр подключён в app.main"
    assert any(
        isinstance(f, RedactInviteTokens) for f in logging.getLogger("uvicorn.access").filters
    )


def test_token_and_link_format() -> None:
    tokens = {new_token() for _ in range(100)}
    assert len(tokens) == 100
    for token in tokens:
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
        assert len(base64.urlsafe_b64decode(token + "=")) == 32
        assert hash_token(token) == hashlib.sha256(token.encode("ascii")).digest()
    token = tokens.pop()
    assert invite_link(BOT, token) == f"https://t.me/{BOT}?startapp={token}"


def test_bot_username_is_validated() -> None:
    with pytest.raises(ValueError):
        Settings(bot_username="bad name/../x")
    assert Settings(bot_username="Feelings_AI_bot").bot_username == "Feelings_AI_bot"


async def test_create_without_bot_username_fails_closed(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Без имени бота ссылку не собрать — пара не создаётся (не остаётся пары без ссылки)."""
    anna = await _person(client, "Анна")

    def _no_bot() -> Settings:
        return Settings(
            telegram_bot_token=SecretStr(TOKEN), session_secret=SecretStr(SECRET), bot_username=""
        )

    app.dependency_overrides[get_settings] = _no_bot
    _error(await client.post("/api/v1/couple", headers=anna.headers), 503, "service_unavailable")
    async with engine.connect() as conn:
        count = await conn.execute(
            text("SELECT count(*) FROM couple_members WHERE user_id = :u"), {"u": anna.id}
        )
        assert count.scalar_one() == 0


async def test_service_does_not_start_without_bot_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import ConfigError
    from app.main import lifespan

    monkeypatch.setenv("FEELINGS_TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("FEELINGS_SESSION_SECRET", SECRET)
    monkeypatch.setenv("FEELINGS_BOT_USERNAME", "")
    saved = app.state.sessionmaker
    app.state.sessionmaker = None
    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigError):
            async with lifespan(app):
                pytest.fail("сервис запустился без имени бота")
        assert app.state.sessionmaker is None
    finally:
        get_settings.cache_clear()
        app.state.sessionmaker = saved


# --- Раунд 1 ревью 1.3 -------------------------------------------------------------------


async def test_leave_allowed_after_age_revoked(client: httpx.AsyncClient) -> None:
    """АР-17: снявший 18+ (ТЗ §9.3) может выйти из пары; пара распускается для обоих."""
    anna, igor, _ = await _active_pair(client)
    revoked = await client.put(
        "/api/v1/me/onboarding",
        headers=igor.headers,
        json={
            "name": "Игорь",
            "address_form": "на ты",
            "partner_name": "Анна",
            "consent_terms": True,
            "consent_data": True,
            "consent_sensitive": True,
            "age_confirmed": False,
        },
    )
    _error(revoked, 403, "underage")
    _error(await client.get("/api/v1/couple", headers=igor.headers), 403, "onboarding_required")
    assert (await client.post("/api/v1/couple/leave", headers=igor.headers)).status_code == 204
    _error(await client.get("/api/v1/couple", headers=anna.headers), 404)
    _error(await client.post("/api/v1/couple/leave", headers=igor.headers), 404)
    # Анна может создать новую пару.
    await _create(client, anna)


async def test_leave_before_onboarding_without_couple_is_404(client: httpx.AsyncClient) -> None:
    newcomer = await _login(client)
    _error(await client.post("/api/v1/couple/leave", headers=newcomer.headers), 404)


async def test_db_onboarding_refusal_maps_to_403(
    client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FB008 от функции БД → 403 onboarding_required (не 401), даже если проверка API устарела."""
    from app.repositories import users

    anna, igor = await _person(client, "Анна"), await _person(client, "Игорь")
    token = await _create(client, anna)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET age_confirmed_at = NULL WHERE id = :u"), {"u": igor.id}
        )

    async def _stale(*_: object) -> bool:
        return True

    monkeypatch.setattr(users, "is_onboarded", _stale)
    resp = await client.post(f"/api/v1/invites/{token}/accept", headers=igor.headers)
    _error(resp, 403, "onboarding_required")
    async with engine.connect() as conn:
        members = await conn.execute(
            text("SELECT count(*) FROM couple_members WHERE user_id = :u"), {"u": igor.id}
        )
        assert members.scalar_one() == 0


async def test_token_collision_rolls_back_couple(
    client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сбой app_issue_invite внутри POST /couple (23505 хеша токена) — не «уже в паре»:
    500, пара не создаётся (одна транзакция), параметры запроса в текст ошибки не попадают."""
    import app.api.couple as couple_api

    anna, olga = await _person(client, "Анна"), await _person(client, "Ольга")
    taken = await _create(client, anna)
    monkeypatch.setattr(couple_api, "new_token", lambda: taken)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as raw:
        resp = await raw.post("/api/v1/couple", headers=olga.headers)
    assert resp.status_code == 500
    assert "already_in_couple" not in resp.text
    async with engine.connect() as conn:
        members = await conn.execute(
            text("SELECT count(*) FROM couple_members WHERE user_id = :u"), {"u": olga.id}
        )
        assert members.scalar_one() == 0
        orphans = await conn.execute(
            text(
                "SELECT count(*) FROM couples c WHERE NOT EXISTS "
                "(SELECT 1 FROM couple_members cm WHERE cm.couple_id = c.id)"
            )
        )
        assert orphans.scalar_one() == 0

    from sqlalchemy.exc import IntegrityError

    from app.repositories import couples

    with pytest.raises(IntegrityError) as ei:
        await couples.create_with_invite(sessionmaker, olga.id, hash_token(taken))
    assert "uq_invites_token_hash" in str(ei.value) or "token_hash" in str(ei.value)
    assert hash_token(taken).hex() not in str(ei.value)
    assert "hide_parameters=True" in str(ei.value)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/invites/"),
        ("get", "/api/v1/invites/aaaa%2Fbbbb"),
        ("get", "/api/v1/invites/aaaa/bbbb"),
        ("post", "/api/v1/invites/a/b/accept"),
        ("post", "/api/v1/invites//accept"),
        ("post", "/api/v1/invites/aaaa%2Fbbbb/accept"),
        ("get", "/api/v1/no-such-route"),
    ],
)
async def test_unrouted_paths_answer_like_unknown_token(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    """D-2 (1.3-test): пустой токен, «/» или %2F — тот же 404, что у неизвестного токена."""
    igor = await _person(client, "Игорь")
    reference = await client.get(f"/api/v1/invites/{new_token()}", headers=igor.headers)
    resp = await client.request(method, path, headers=igor.headers)
    _error(resp, 404)
    assert resp.json() == reference.json()
    assert resp.headers["content-type"] == reference.headers["content-type"]


async def test_wrong_method_is_405_error_schema(client: httpx.AsyncClient) -> None:
    resp = await client.delete("/api/v1/couple")
    _error(resp, 405, "method_not_allowed")
    assert "GET" in resp.headers["allow"]

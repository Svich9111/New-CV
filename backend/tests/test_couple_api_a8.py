"""A8, задача 1.3: независимые HTTP-тесты пар и приглашений (ФТ-03, ФТ-04).

ТЗ §4.1 п.3–6 (ссылка, одноразовость, 7 дней, одна пара), §4.5 п.2 (выход), §7 (правила
доступа), §9.3 (18+); DECISIONS.md АР-13, АР-16; контракт api/openapi.yaml.
Хелперы A2 (tests/test_couple_api.py) не используются: вход, онбординг, разбор ссылки и
проверка схем — свои. Данные выдуманные (CLAUDE.md). Дефекты — xfail(strict=True) со
ссылкой на docs/reviews/1.3-test.md."""

import asyncio
import base64
import hashlib
import json
import logging
import statistics
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import uvicorn
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.core.telegram import sign_init_data
from app.main import app
from tests.contract import SPEC, assert_matches

BOT_TOKEN = "777000:A8-couple-bot-token-not-real"
SESSION_SECRET = "a8-couple-session-secret-" + "x" * 20
BOT = "A8_Feelings_bot"
API = "/api/v1"


def _settings(bot: str = BOT) -> Settings:
    return Settings(
        telegram_bot_token=SecretStr(BOT_TOKEN),
        session_secret=SecretStr(SESSION_SECRET),
        session_ttl_s=900,
        bot_username=bot,
    )


@pytest.fixture
async def api(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[httpx.AsyncClient]:
    app.state.sessionmaker = sessionmaker
    app.dependency_overrides[get_settings] = _settings
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a8") as c:
        yield c
    app.dependency_overrides.clear()


# --- Свои хелперы: вход по initData, онбординг, разбор ссылки ------------------------------


class Who:
    def __init__(self, name: str, token: str, user_id: str) -> None:
        self.name, self.id = name, user_id
        self.h = {"Authorization": f"Bearer {token}"}


def _tg_id() -> int:
    return 7_100_000_000 + uuid.uuid4().int % 100_000_000


async def _enter(c: httpx.AsyncClient, name: str) -> Who:
    init = sign_init_data(
        {
            "auth_date": str(int(time.time())),
            "user": json.dumps({"id": _tg_id(), "first_name": name}, ensure_ascii=False),
        },
        BOT_TOKEN,
    )
    r = await c.post(f"{API}/auth/telegram", json={"init_data": init})
    assert r.status_code == 200, r.text
    return Who(name, r.json()["access_token"], r.json()["me"]["id"])


def _onboarding(name: str, age: bool = True) -> dict[str, object]:
    return {
        "name": name,
        "address_form": "на ты",
        "partner_name": "Партнёр",
        "consent_terms": True,
        "consent_data": True,
        "consent_sensitive": True,
        "age_confirmed": age,
    }


async def _onboard(c: httpx.AsyncClient, who: Who, age: bool = True) -> httpx.Response:
    return await c.put(f"{API}/me/onboarding", headers=who.h, json=_onboarding(who.name, age))


async def _adult(c: httpx.AsyncClient, name: str) -> Who:
    who = await _enter(c, name)
    r = await _onboard(c, who)
    assert r.status_code == 200, r.text
    return who


def _link_token(link: str) -> str:
    """Ссылка — ровно https://t.me/<бот>?startapp=<токен>; токен 43 символа base64url."""
    parts = urlsplit(link)
    assert (parts.scheme, parts.netloc, parts.path, parts.fragment) == (
        "https",
        "t.me",
        f"/{BOT}",
        "",
    )
    query = parse_qs(parts.query, strict_parsing=True)
    assert list(query) == ["startapp"], query
    (token,) = query["startapp"]
    assert len(token) == 43, token
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    raw = base64.urlsafe_b64decode(token + "=")
    assert len(raw) == 32
    assert base64.urlsafe_b64encode(raw).rstrip(b"=").decode() == token  # каноничная запись
    assert link == f"https://t.me/{BOT}?startapp={token}"
    return token


# --- Контракт: схема + никаких лишних полей + статус объявлен --------------------------------


def _schema(name: str) -> dict[str, Any]:
    node: dict[str, Any] = SPEC["components"]["schemas"][name]
    return node


def _no_extra_keys(instance: Any, schema: dict[str, Any]) -> None:
    if isinstance(instance, dict) and "properties" in schema:
        extra = set(instance) - set(schema["properties"])
        assert not extra, extra
        for key, value in instance.items():
            _no_extra_keys(value, schema["properties"][key])
    elif isinstance(instance, list) and "items" in schema:
        for item in instance:
            _no_extra_keys(item, schema["items"])


def _declared(method: str, path: str) -> set[str]:
    return set(SPEC["paths"][path][method]["responses"])


def _ok(r: httpx.Response, status: int, schema: str | None) -> Any:
    assert r.status_code == status, r.text
    if schema is None:
        assert r.content == b""
        return None
    body = r.json()
    assert_matches(body, schema)
    _no_extra_keys(body, _schema(schema))
    return body


def _err(r: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, r.text
    body: dict[str, Any] = r.json()
    assert_matches(body, "Error")
    assert set(body) == {"code", "message"}
    assert body["code"] == code, body
    return body


PREVIEW_SCHEMA = SPEC["paths"]["/invites/{token}"]["get"]["responses"]["200"]["content"][
    "application/json"
]["schema"]


def _preview(r: httpx.Response) -> dict[str, Any]:
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    assert set(body) == set(PREVIEW_SCHEMA["properties"]) == {"inviter_name", "expires_at"}
    assert body["inviter_name"] is None or isinstance(body["inviter_name"], str)
    datetime.fromisoformat(body["expires_at"])
    return body


async def _create(c: httpx.AsyncClient, who: Who) -> tuple[str, str]:
    body = _ok(await c.post(f"{API}/couple", headers=who.h), 201, "InviteCreated")
    return body["couple_id"], _link_token(body["link"])


async def _pair(c: httpx.AsyncClient, a: str = "Анна", b: str = "Игорь") -> tuple[Who, Who, str]:
    x, y = await _adult(c, a), await _adult(c, b)
    couple_id, token = await _create(c, x)
    _ok(await c.post(f"{API}/invites/{token}/accept", headers=y.h), 200, "Couple")
    return x, y, couple_id


async def _db(engine: AsyncEngine, sql: str, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


# --- Полный сценарий (критерий 8; ТЗ §4.1 п.3–6, §4.5 п.2) ---------------------------------


async def test_full_scenario_over_http(api: httpx.AsyncClient, engine: AsyncEngine) -> None:
    anna = await _adult(api, "Анна")
    _err(await api.get(f"{API}/couple", headers=anna.h), 404, "not_found")

    created = _ok(await api.post(f"{API}/couple", headers=anna.h), 201, "InviteCreated")
    token = _link_token(created["link"])
    couple_id = created["couple_id"]
    expires = datetime.fromisoformat(created["expires_at"])
    assert expires.tzinfo is not None
    assert timedelta(days=7) - timedelta(minutes=1) < expires - datetime.now(UTC) <= timedelta(7)

    pending = _ok(await api.get(f"{API}/couple", headers=anna.h), 200, "Couple")
    assert pending == {
        "id": couple_id,
        "status": "pending",
        "members": [{"role": "initiator", "name": "Анна", "is_me": True}],
    }
    me = (await api.get(f"{API}/me", headers=anna.h)).json()
    assert me["has_active_couple"] is False  # ожидающая пара — ещё не активная

    # Игорь входит по ссылке, видит только имя пригласившего и срок — до онбординга (§4.1 п.4).
    igor = await _enter(api, "Игорь")
    preview = _preview(await api.get(f"{API}/invites/{token}", headers=igor.h))
    assert preview["inviter_name"] == "Анна"
    assert datetime.fromisoformat(preview["expires_at"]) == expires
    _err(
        await api.post(f"{API}/invites/{token}/accept", headers=igor.h), 403, "onboarding_required"
    )
    assert (await _onboard(api, igor)).status_code == 200
    accepted = _ok(await api.post(f"{API}/invites/{token}/accept", headers=igor.h), 200, "Couple")
    assert accepted == {
        "id": couple_id,
        "status": "active",
        "members": [
            {"role": "initiator", "name": "Анна", "is_me": False},
            {"role": "partner", "name": "Игорь", "is_me": True},
        ],
    }
    for who in (anna, igor):
        body = _ok(await api.get(f"{API}/couple", headers=who.h), 200, "Couple")
        assert body["id"] == couple_id
        assert body["status"] == "active"
        assert [m["is_me"] for m in body["members"]] == [who is anna, who is igor]
        assert (await api.get(f"{API}/me", headers=who.h)).json()["has_active_couple"] is True

    # Ссылка одноразовая (§4.1 п.6).
    _err(await api.get(f"{API}/invites/{token}", headers=igor.h), 410, "invite_used")
    olga = await _adult(api, "Ольга")
    _err(await api.post(f"{API}/invites/{token}/accept", headers=olga.h), 410, "invite_used")

    # Выход одного распускает пару для обоих (§4.5 п.2, АР-13); /couple → 404 (АР-16).
    _ok(await api.post(f"{API}/couple/leave", headers=igor.h), 204, None)
    for who in (anna, igor):
        _err(await api.get(f"{API}/couple", headers=who.h), 404, "not_found")
        _err(await api.post(f"{API}/couple/leave", headers=who.h), 404, "not_found")
    assert await _db(engine, "SELECT status::text FROM couples WHERE id = :c", c=couple_id) == (
        "dissolved"
    )
    # Агенты и память сохраняются (§4.5 п.2).
    for who in (anna, igor):
        assert await _db(engine, "SELECT count(*) FROM agents WHERE user_id = :u", u=who.id) == 1

    # Оба могут создать новую пару; новые пары разные и ожидающие.
    new_a, tok_a = await _create(api, anna)
    new_i, tok_i = await _create(api, igor)
    assert len({new_a, new_i, couple_id}) == 3
    assert tok_a != tok_i != token
    for who, cid in ((anna, new_a), (igor, new_i)):
        body = _ok(await api.get(f"{API}/couple", headers=who.h), 200, "Couple")
        assert (body["id"], body["status"], len(body["members"])) == (cid, "pending", 1)
    # и снова соединиться друг с другом: Игорь выходит из своей ожидающей и принимает ссылку Анны
    _ok(await api.post(f"{API}/couple/leave", headers=igor.h), 204, None)
    again = _ok(await api.post(f"{API}/invites/{tok_a}/accept", headers=igor.h), 200, "Couple")
    assert (again["id"], again["status"]) == (new_a, "active")


# --- Отрицательные сценарии: повтор, срок, вторая пара, своя ссылка (RoadMap 1.3) -----------


async def test_expired_link_is_gone_for_view_and_accept(
    api: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    anna, igor = await _adult(api, "Анна"), await _adult(api, "Игорь")
    couple_id, token = await _create(api, anna)
    async with engine.begin() as conn:  # срок вышел, статус ещё active (часы БД, а не фон)
        await conn.execute(
            text(
                "UPDATE invites SET expires_at = now() - interval '1 second' WHERE couple_id = :c"
            ),
            {"c": couple_id},
        )
    _err(await api.get(f"{API}/invites/{token}", headers=igor.h), 410, "invite_expired")
    _err(await api.post(f"{API}/invites/{token}/accept", headers=igor.h), 410, "invite_expired")
    _err(await api.get(f"{API}/couple", headers=igor.h), 404, "not_found")
    # Анна выпускает новую ссылку — она работает.
    new = _ok(await api.post(f"{API}/couple/invite", headers=anna.h), 201, "InviteCreated")
    assert new["couple_id"] == couple_id
    fresh = _link_token(new["link"])
    _ok(await api.post(f"{API}/invites/{fresh}/accept", headers=igor.h), 200, "Couple")


async def test_reissue_revokes_old_link(api: httpx.AsyncClient) -> None:
    anna, igor = await _adult(api, "Анна"), await _adult(api, "Игорь")
    _, old = await _create(api, anna)
    new = _link_token(
        _ok(await api.post(f"{API}/couple/invite", headers=anna.h), 201, "InviteCreated")["link"]
    )
    assert new != old
    _err(await api.get(f"{API}/invites/{old}", headers=igor.h), 410, "invite_used")
    _err(await api.post(f"{API}/invites/{old}/accept", headers=igor.h), 410, "invite_used")
    _preview(await api.get(f"{API}/invites/{new}", headers=igor.h))


async def test_second_couple_and_foreign_link_conflict(
    api: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Одна пара на пользователя (§4.1 п.6, ФТ-04): ни вторую создать, ни чужую принять, ни
    в активной паре выпустить ссылку; своя ссылка — own_invite."""
    anna, igor, couple_id = await _pair(api)
    olga = await _adult(api, "Ольга")
    _, olga_token = await _create(api, olga)
    for who in (anna, igor):
        _err(await api.post(f"{API}/couple", headers=who.h), 409, "already_in_couple")
        _err(
            await api.post(f"{API}/invites/{olga_token}/accept", headers=who.h),
            409,
            "already_in_couple",
        )
        _err(await api.post(f"{API}/couple/invite", headers=who.h), 409, "couple_not_pending")
    _err(await api.post(f"{API}/invites/{olga_token}/accept", headers=olga.h), 409, "own_invite")
    # Пользователь в своей ожидающей паре тоже не принимает чужую (сначала выйти).
    pavel = await _adult(api, "Павел")
    await _create(api, pavel)
    _err(
        await api.post(f"{API}/invites/{olga_token}/accept", headers=pavel.h),
        409,
        "already_in_couple",
    )
    _preview(await api.get(f"{API}/invites/{olga_token}", headers=pavel.h))  # ссылка цела
    n = await _db(
        engine,
        "SELECT count(*) FROM couple_members WHERE user_id = ANY(:u) AND active",
        u=[uuid.UUID(w.id) for w in (anna, igor, olga, pavel)],
    )
    assert n == 4
    assert await _db(engine, "SELECT count(*) FROM couples WHERE id = :c", c=couple_id) == 1


async def test_leave_pending_couple_revokes_link(api: httpx.AsyncClient) -> None:
    anna, igor = await _adult(api, "Анна"), await _adult(api, "Игорь")
    _, token = await _create(api, anna)
    _ok(await api.post(f"{API}/couple/leave", headers=anna.h), 204, None)
    _err(await api.get(f"{API}/invites/{token}", headers=igor.h), 410, "invite_used")
    _err(await api.post(f"{API}/invites/{token}/accept", headers=igor.h), 410, "invite_used")
    _err(await api.post(f"{API}/couple/invite", headers=anna.h), 404, "not_found")


# --- Посторонний, партнёр, 404 без различий (§7 «Правила доступа», §9.2) --------------------


async def test_outsider_sees_nothing_and_404_bodies_are_identical(api: httpx.AsyncClient) -> None:
    anna, igor, couple_id = await _pair(api)
    olga = await _adult(api, "Ольга")
    bodies = [
        _err(await api.get(f"{API}/couple", headers=olga.h), 404, "not_found"),
        _err(await api.post(f"{API}/couple/leave", headers=olga.h), 404, "not_found"),
        _err(await api.post(f"{API}/couple/invite", headers=olga.h), 404, "not_found"),
        _err(await api.get(f"{API}/invites/{'Q' * 43}", headers=olga.h), 404, "not_found"),
        _err(await api.get(f"{API}/invites/{couple_id}", headers=olga.h), 404, "not_found"),
        _err(await api.post(f"{API}/invites/{'Q' * 43}/accept", headers=olga.h), 404, "not_found"),
    ]
    assert all(b == bodies[0] for b in bodies), bodies
    for b in bodies:
        for leak in ("Анна", "Игорь", anna.id, igor.id, couple_id):
            assert leak not in json.dumps(b, ensure_ascii=False)
    # Пара Анны и Игоря не изменилась.
    body = _ok(await api.get(f"{API}/couple", headers=anna.h), 200, "Couple")
    assert body["status"] == "active"


async def test_members_reveal_only_role_name_is_me(api: httpx.AsyncClient) -> None:
    """Партнёр видит о другом только роль и имя (не id, не telegram_id, не согласия)."""
    anna, igor, _ = await _pair(api)
    for who, other in ((anna, igor), (igor, anna)):
        r = await api.get(f"{API}/couple", headers=who.h)
        body = _ok(r, 200, "Couple")
        for m in body["members"]:
            assert set(m) == {"role", "name", "is_me"}
        assert other.id not in r.text and who.id not in r.text


async def test_invite_preview_reveals_only_name_and_expiry(api: httpx.AsyncClient) -> None:
    anna = await _adult(api, "Анна")
    couple_id, token = await _create(api, anna)
    viewer = await _enter(api, "Гость")  # без онбординга
    r = await api.get(f"{API}/invites/{token}", headers=viewer.h)
    _preview(r)
    for leak in (couple_id, anna.id, "initiator", "pending", "active", "token"):
        assert leak not in r.text, leak
    # Просмотр ничего не меняет: ссылка остаётся рабочей.
    _preview(await api.get(f"{API}/invites/{token}", headers=viewer.h))


async def test_forged_or_deleted_session_is_401(
    api: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    anna = await _adult(api, "Анна")
    _, token = await _create(api, anna)
    calls: list[tuple[str, str]] = [
        ("get", "/couple"),
        ("post", "/couple"),
        ("post", "/couple/invite"),
        ("post", "/couple/leave"),
        ("get", f"/invites/{token}"),
        ("post", f"/invites/{token}/accept"),
    ]
    for headers in ({}, {"Authorization": "Bearer x.y.z"}, {"Authorization": "Basic Zm9vOmJhcg=="}):
        for method, path in calls:
            _err(await api.request(method, API + path, headers=headers), 401, "unauthorized")
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": anna.id}
        )
    for method, path in calls:
        _err(await api.request(method, API + path, headers=anna.h), 401, "unauthorized")


# --- Отзыв 18+ через PUT /me/onboarding (ТЗ §9.3; карточка, критерий 2; АР-13) -------------


async def test_age_revocation_closes_couple_endpoints_fact(
    api: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Фиксация фактического поведения (см. отчёт, D-3): после «мне нет 18» закрыты все
    эндпоинты пары, включая выход; открыт только просмотр приглашения. Пара остаётся
    активной, партнёр её видит и может выйти. Повторное «да» (АР-14) возвращает доступ."""
    anna, igor, couple_id = await _pair(api)
    _err(await _onboard(api, anna, age=False), 403, "underage")
    olga = await _adult(api, "Ольга")
    _, olga_token = await _create(api, olga)
    for method, path in (
        ("get", "/couple"),
        ("post", "/couple"),
        ("post", "/couple/invite"),
        ("post", "/couple/leave"),
        ("post", f"/invites/{olga_token}/accept"),
    ):
        _err(await api.request(method, API + path, headers=anna.h), 403, "onboarding_required")
    _preview(await api.get(f"{API}/invites/{olga_token}", headers=anna.h))
    # Пара не тронута, партнёр видит её активной.
    body = _ok(await api.get(f"{API}/couple", headers=igor.h), 200, "Couple")
    assert (body["id"], body["status"]) == (couple_id, "active")
    assert (
        await _db(engine, "SELECT count(*) FROM safety_events WHERE user_id = :u", u=anna.id) == 1
    )
    # Повторное «да» — доступ возвращается, выход работает.
    assert (await _onboard(api, anna)).status_code == 200
    _ok(await api.post(f"{API}/couple/leave", headers=anna.h), 204, None)


async def test_age_revocation_in_pending_couple_blocks_new_links(
    api: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """D-4 (0.7b) по HTTP: участник ожидающей пары после «мне нет 18» ссылок не выпускает;
    прежняя ссылка остаётся (принять её может совершеннолетний — пара не распускается)."""
    anna, igor = await _adult(api, "Анна"), await _adult(api, "Игорь")
    couple_id, token = await _create(api, anna)
    _err(await _onboard(api, anna, age=False), 403, "underage")
    _err(await api.post(f"{API}/couple/invite", headers=anna.h), 403, "onboarding_required")
    assert (
        await _db(
            engine,
            "SELECT count(*) FROM invites WHERE couple_id = :c AND status = 'active'",
            c=couple_id,
        )
        == 1
    )
    _preview(await api.get(f"{API}/invites/{token}", headers=igor.h))


# --- Токен: перебор не отличает «нет» от «неверного формата» (§9.1, ФТ-03) ------------------


MALFORMED = [
    "a" * 31,
    "a" * 65,
    "a" * 42 + "=",
    "a" * 42 + "+",
    "a" * 42 + ".",
    "%2E" * 20,
    "токен" * 10,
    "a" * 10_000,
]


async def test_bruteforce_cannot_tell_missing_from_malformed(api: httpx.AsyncClient) -> None:
    guest = await _adult(api, "Перебор")
    reference: dict[str, Any] | None = None
    missing = base64.urlsafe_b64encode(uuid.uuid4().bytes * 2).rstrip(b"=").decode()
    for token in [missing, *MALFORMED]:
        for method, suffix in (("get", ""), ("post", "/accept")):
            r = await api.request(method, f"{API}/invites/{token}{suffix}", headers=guest.h)
            body = _err(r, 404, "not_found")
            reference = reference or body
            assert body == reference, (token[:20], body)
            assert r.headers.get("content-type") == "application/json"


async def test_bruteforce_timing_is_similar(api: httpx.AsyncClient) -> None:
    """Примерное время: неверный формат отвечает без запроса к БД — разница должна быть
    малой по сравнению с самим запросом. Порог грубый (CI), фактические медианы — в отчёте."""
    guest = await _adult(api, "Время")

    async def median(tokens: list[str]) -> float:
        samples = []
        for t in tokens:
            start = time.perf_counter()
            r = await api.get(f"{API}/invites/{t}", headers=guest.h)
            samples.append(time.perf_counter() - start)
            assert r.status_code == 404
        return statistics.median(samples)

    missing = [
        base64.urlsafe_b64encode(uuid.uuid4().bytes * 2).rstrip(b"=").decode() for _ in range(40)
    ]
    malformed = ["*" + m[1:] for m in missing]
    await median(missing[:5])  # прогрев
    m_missing, m_malformed = await median(missing), await median(malformed)
    print(f"A8 timing: missing={m_missing * 1000:.2f}ms malformed={m_malformed * 1000:.2f}ms")
    assert abs(m_missing - m_malformed) < 0.025


@pytest.mark.xfail(
    strict=True,
    reason="D-2 (1.3-test): токен с «/» (в т.ч. %2F) или пустой токен уходит мимо маршрута — "
    "Starlette отвечает 404 {'detail': 'Not Found'} не по схеме Error и иначе, чем на "
    "несуществующий токен",
)
@pytest.mark.parametrize("path", ["/invites/", "/invites/aaaa%2Fbbbb", "/invites/a/b/accept"])
async def test_token_with_slash_answers_like_missing(api: httpx.AsyncClient, path: str) -> None:
    guest = await _adult(api, "Слэш")
    reference = (await api.get(f"{API}/invites/{'Q' * 43}", headers=guest.h)).json()
    method = "post" if path.endswith("/accept") else "get"
    r = await api.request(method, API + path, headers=guest.h)
    assert r.status_code == 404
    assert r.json() == reference


# --- Токен нигде не хранится открыто: все таблицы и журналы (§9.1; карточка, критерий 5) ----


async def _all_rows_dump(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        tables = (
            await conn.execute(
                text(
                    "SELECT quote_ident(table_schema) || '.' || quote_ident(table_name) "
                    "FROM information_schema.tables "
                    "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
                    "AND table_type = 'BASE TABLE'"
                )
            )
        ).scalars()
        chunks = []
        for t in list(tables):
            rows = await conn.execute(text(f"SELECT row_to_json(x)::text FROM {t} x"))
            chunks.extend(r for r in rows.scalars())
    return "\n".join(chunks)


async def test_token_not_stored_in_any_table(api: httpx.AsyncClient, engine: AsyncEngine) -> None:
    anna, igor = await _adult(api, "Анна"), await _adult(api, "Игорь")
    tokens = [(await _create(api, anna))[1]]
    for _ in range(2):
        link = (await api.post(f"{API}/couple/invite", headers=anna.h)).json()["link"]
        tokens.append(_link_token(link))
    await api.get(f"{API}/invites/{tokens[-1]}", headers=igor.h)
    await api.post(f"{API}/invites/{tokens[-1]}/accept", headers=igor.h)
    _ok(await api.post(f"{API}/couple/leave", headers=igor.h), 204, None)
    dump = await _all_rows_dump(engine)
    assert "couple.leave" in dump  # дамп охватывает и audit_log
    for token in tokens:
        raw = base64.urlsafe_b64decode(token + "=")
        for form in (
            token,
            token[:20],
            token.encode().hex(),
            raw.hex(),
            base64.b64encode(raw).decode().rstrip("="),
        ):
            assert form not in dump, form
        h = hashlib.sha256(token.encode()).digest()
        assert await _db(engine, "SELECT count(*) FROM invites WHERE token_hash = :h", h=h) == 1


async def test_token_not_in_logs_including_uvicorn_access(
    sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    """Настоящий uvicorn с журналом доступа: путь /invites/<токен> в журнал не попадает."""
    caplog.set_level(logging.DEBUG)
    app.state.sessionmaker = sessionmaker
    app.dependency_overrides[get_settings] = _settings
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=0, log_config=None, lifespan="off", access_log=True
        )
    )
    task = asyncio.ensure_future(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as c:
            anna, igor = await _adult(c, "Анна"), await _adult(c, "Игорь")
            _, token = await _create(c, anna)
            await c.get(f"{API}/invites/{token}", headers=igor.h)
            await c.get(f"{API}/invites/{token}?x=1", headers=igor.h)
            await c.post(f"{API}/invites/{token}/accept", headers=igor.h)
            await c.post(f"{API}/invites/{token}/accept", headers=igor.h)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        app.dependency_overrides.clear()
    access = [r for r in caplog.records if r.name == "uvicorn.access"]
    assert len(access) >= 6, [r.name for r in caplog.records]
    assert any("/invites/***" in r.getMessage() for r in access)
    for r in caplog.records:
        if r.name.startswith(("httpx", "httpcore")):
            continue  # сторона клиента теста
        assert token not in r.getMessage(), r.name
        assert token not in repr(r.__dict__), r.name


# --- Гонки по HTTP (ФТ-03, ФТ-04; АР-13 — блокировки пользователь → приглашения → пара) -----


async def _state(engine: AsyncEngine, couple_id: str) -> tuple[str, int]:
    async with engine.connect() as conn:
        status = (
            await conn.execute(
                text("SELECT status::text FROM couples WHERE id = :c"), {"c": couple_id}
            )
        ).scalar_one()
        n = (
            await conn.execute(
                text("SELECT count(*) FROM couple_members WHERE couple_id = :c AND active"),
                {"c": couple_id},
            )
        ).scalar_one()
    return status, n


async def _gather(*aws: Awaitable[httpx.Response]) -> list[httpx.Response]:
    return list(await asyncio.gather(*aws))


@pytest.mark.parametrize("round_", range(4))
async def test_race_two_accepts_one_link(
    api: httpx.AsyncClient, engine: AsyncEngine, round_: int
) -> None:
    a, i, p = [await _adult(api, n) for n in ("Анна", "Игорь", "Павел")]
    couple_id, token = await _create(api, a)
    r1, r2 = await _gather(
        api.post(f"{API}/invites/{token}/accept", headers=i.h),
        api.post(f"{API}/invites/{token}/accept", headers=p.h),
    )
    assert sorted([r1.status_code, r2.status_code]) == [200, 410], (r1.text, r2.text)
    loser = r1 if r1.status_code == 410 else r2
    _err(loser, 410, "invite_used")
    assert await _state(engine, couple_id) == ("active", 2)


@pytest.mark.parametrize("round_", range(4))
async def test_race_accept_vs_leave(
    api: httpx.AsyncClient, engine: AsyncEngine, round_: int
) -> None:
    anna, igor = await _adult(api, "Анна"), await _adult(api, "Игорь")
    couple_id, token = await _create(api, anna)
    leave, accept = await _gather(
        api.post(f"{API}/couple/leave", headers=anna.h),
        api.post(f"{API}/invites/{token}/accept", headers=igor.h),
    )
    assert leave.status_code == 204, leave.text
    if accept.status_code == 200:
        assert_matches(accept.json(), "Couple")
    else:
        _err(accept, 410, "invite_used")
    # В любом порядке пара распущена для обоих, никто не «заперт».
    assert await _state(engine, couple_id) == ("dissolved", 0)
    for who in (anna, igor):
        _err(await api.get(f"{API}/couple", headers=who.h), 404, "not_found")
        await _create(api, who)


@pytest.mark.parametrize("round_", range(4))
async def test_race_double_create_same_person(
    api: httpx.AsyncClient, engine: AsyncEngine, round_: int
) -> None:
    anna = await _adult(api, "Анна")
    r1, r2 = await _gather(
        api.post(f"{API}/couple", headers=anna.h), api.post(f"{API}/couple", headers=anna.h)
    )
    assert sorted([r1.status_code, r2.status_code]) == [201, 409], (r1.text, r2.text)
    winner, loser = (r1, r2) if r1.status_code == 201 else (r2, r1)
    _err(loser, 409, "already_in_couple")
    assert_matches(winner.json(), "InviteCreated")
    assert (
        await _db(engine, "SELECT count(*) FROM couple_members WHERE user_id = :u", u=anna.id) == 1
    )
    n_inv = await _db(
        engine,
        "SELECT count(*) FROM invites WHERE created_by = :u AND status = 'active'",
        u=anna.id,
    )
    assert n_inv == 1
    # Пары без ссылки не осталось: никакая «лишняя» pending-пара не создана.
    orphan = await _db(
        engine,
        "SELECT count(*) FROM couples c WHERE NOT EXISTS "
        "(SELECT 1 FROM couple_members m WHERE m.couple_id = c.id)",
    )
    assert orphan == 0


async def test_race_double_reissue_leaves_one_active_link(
    api: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    anna, igor = await _adult(api, "Анна"), await _adult(api, "Игорь")
    couple_id, first = await _create(api, anna)
    r1, r2 = await _gather(
        api.post(f"{API}/couple/invite", headers=anna.h),
        api.post(f"{API}/couple/invite", headers=anna.h),
    )
    assert [r1.status_code, r2.status_code] == [201, 201], (r1.text, r2.text)
    n = await _db(
        engine,
        "SELECT count(*) FROM invites WHERE couple_id = :c AND status = 'active'",
        c=couple_id,
    )
    assert n == 1
    tokens = [_link_token(r.json()["link"]) for r in (r1, r2)]
    live = []
    for t in [first, *tokens]:
        r = await api.get(f"{API}/invites/{t}", headers=igor.h)
        if r.status_code == 200:
            live.append(t)
        else:
            _err(r, 410, "invite_used")
    assert len(live) == 1 and live[0] in tokens


# --- Б-13 по факту: у feelings_app на парах и приглашениях только SELECT (миграция 0004) ----


async def test_app_role_has_only_select_on_couple_tables(engine: AsyncEngine) -> None:
    """Ни на таблицу, ни на любую колонку нет INSERT/UPDATE; DELETE/TRUNCATE нет; политики
    RLS на этих таблицах — только чтение (АР-13, миграция 0004)."""
    async with engine.connect() as conn:
        for table in ("couples", "couple_members", "invites"):

            async def has(sql: str, priv: str, t: str = table) -> bool:
                q = text(sql)
                return bool((await conn.execute(q, {"t": t, "p": priv})).scalar_one())

            assert await has("SELECT has_table_privilege('feelings_app', :t, :p)", "SELECT")
            for priv in ("INSERT", "UPDATE", "REFERENCES"):
                assert not await has(
                    "SELECT has_any_column_privilege('feelings_app', :t, :p)", priv
                ), (table, priv)
            for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "TRIGGER"):
                assert not await has("SELECT has_table_privilege('feelings_app', :t, :p)", priv), (
                    table,
                    priv,
                )
            cmds = (
                await conn.execute(
                    text(
                        "SELECT coalesce(array_agg(cmd::text), '{}') FROM pg_policies "
                        "WHERE schemaname = 'public' AND tablename = :t"
                    ),
                    {"t": table},
                )
            ).scalar_one()
            assert set(cmds) <= {"SELECT"}, (table, cmds)


# --- Каждый фактический статус объявлен в контракте (карточка, критерий 3) ------------------

Scenario = Callable[[httpx.AsyncClient, AsyncEngine], Awaitable[httpx.Response]]


async def _s_get_couple_200(c: httpx.AsyncClient, e: AsyncEngine) -> httpx.Response:
    a, _, _ = await _pair(c)
    return await c.get(f"{API}/couple", headers=a.h)


async def _s_get_couple_404(c: httpx.AsyncClient, e: AsyncEngine) -> httpx.Response:
    return await c.get(f"{API}/couple", headers=(await _adult(c, "Н")).h)


def _no_session(method: str, path: str) -> Scenario:
    async def run(c: httpx.AsyncClient, e: AsyncEngine) -> httpx.Response:
        return await c.request(method, API + path.format(t="Q" * 43))

    return run


def _newcomer(method: str, path: str) -> Scenario:
    async def run(c: httpx.AsyncClient, e: AsyncEngine) -> httpx.Response:
        _, token = await _create(c, await _adult(c, "Инициатор"))
        who = await _enter(c, "Новичок")
        return await c.request(method, API + path.format(t=token), headers=who.h)

    return run


def _no_bot(path: str) -> Scenario:
    async def run(c: httpx.AsyncClient, e: AsyncEngine) -> httpx.Response:
        who = await _adult(c, "Безбота")
        if path == "/couple/invite":
            await _create(c, who)
        app.dependency_overrides[get_settings] = lambda: _settings(bot="")
        try:
            return await c.post(API + path, headers=who.h)
        finally:
            app.dependency_overrides[get_settings] = _settings

    return run


async def _s_create_201(c: httpx.AsyncClient, e: AsyncEngine) -> httpx.Response:
    return await c.post(f"{API}/couple", headers=(await _adult(c, "А")).h)


async def _s_create_409(c: httpx.AsyncClient, e: AsyncEngine) -> httpx.Response:
    a, _, _ = await _pair(c)
    return await c.post(f"{API}/couple", headers=a.h)


async def _s_invite_201(c: httpx.AsyncClient, e: AsyncEngine) -> httpx.Response:
    a = await _adult(c, "А")
    await _create(c, a)
    return await c.post(f"{API}/couple/invite", headers=a.h)


async def _s_invite_404(c: httpx.AsyncClient, e: AsyncEngine) -> httpx.Response:
    return await c.post(f"{API}/couple/invite", headers=(await _adult(c, "А")).h)


async def _s_invite_409(c: httpx.AsyncClient, e: AsyncEngine) -> httpx.Response:
    a, _, _ = await _pair(c)
    return await c.post(f"{API}/couple/invite", headers=a.h)


async def _s_leave_204(c: httpx.AsyncClient, e: AsyncEngine) -> httpx.Response:
    a, _, _ = await _pair(c)
    return await c.post(f"{API}/couple/leave", headers=a.h)


async def _s_leave_404(c: httpx.AsyncClient, e: AsyncEngine) -> httpx.Response:
    return await c.post(f"{API}/couple/leave", headers=(await _adult(c, "А")).h)


async def _s_view_200(c: httpx.AsyncClient, e: AsyncEngine) -> httpx.Response:
    _, t = await _create(c, await _adult(c, "А"))
    return await c.get(f"{API}/invites/{t}", headers=(await _enter(c, "Б")).h)


async def _s_view_404(c: httpx.AsyncClient, e: AsyncEngine) -> httpx.Response:
    return await c.get(f"{API}/invites/{'Q' * 43}", headers=(await _enter(c, "Б")).h)


async def _s_view_410(c: httpx.AsyncClient, e: AsyncEngine) -> httpx.Response:
    a = await _adult(c, "А")
    _, t = await _create(c, a)
    await c.post(f"{API}/couple/leave", headers=a.h)
    return await c.get(f"{API}/invites/{t}", headers=(await _enter(c, "Б")).h)


async def _s_accept_200(c: httpx.AsyncClient, e: AsyncEngine) -> httpx.Response:
    _, t = await _create(c, await _adult(c, "А"))
    return await c.post(f"{API}/invites/{t}/accept", headers=(await _adult(c, "Б")).h)


async def _s_accept_404(c: httpx.AsyncClient, e: AsyncEngine) -> httpx.Response:
    return await c.post(f"{API}/invites/{'Q' * 43}/accept", headers=(await _adult(c, "Б")).h)


async def _s_accept_409(c: httpx.AsyncClient, e: AsyncEngine) -> httpx.Response:
    a = await _adult(c, "А")
    _, t = await _create(c, a)
    return await c.post(f"{API}/invites/{t}/accept", headers=a.h)


async def _s_accept_410(c: httpx.AsyncClient, e: AsyncEngine) -> httpx.Response:
    a = await _adult(c, "А")
    _, t = await _create(c, a)
    await c.post(f"{API}/couple/leave", headers=a.h)
    return await c.post(f"{API}/invites/{t}/accept", headers=(await _adult(c, "Б")).h)


def _undeclared(reason: str) -> pytest.MarkDecorator:
    return pytest.mark.xfail(strict=True, reason=f"D-1 (1.3-test, для A1): {reason}")


GET_C, POST_C, INV, LEAVE = (
    ("get", "/couple"),
    ("post", "/couple"),
    ("post", "/couple/invite"),
    (
        "post",
        "/couple/leave",
    ),
)
VIEW, ACC = ("get", "/invites/{token}"), ("post", "/invites/{token}/accept")
NO401 = "401 unauthorized отдаётся, но в контракте для пути не объявлен"

SERVED: list[Any] = [
    pytest.param(GET_C, 200, _s_get_couple_200, id="GET /couple 200"),
    pytest.param(GET_C, 404, _s_get_couple_404, id="GET /couple 404"),
    pytest.param(
        GET_C,
        403,
        _newcomer("get", "/couple"),
        id="GET /couple 403",
        marks=_undeclared("GET /couple отдаёт 403 onboarding_required — не объявлен"),
    ),
    pytest.param(
        GET_C, 401, _no_session("get", "/couple"), id="GET /couple 401", marks=_undeclared(NO401)
    ),
    pytest.param(POST_C, 201, _s_create_201, id="POST /couple 201"),
    pytest.param(POST_C, 409, _s_create_409, id="POST /couple 409"),
    pytest.param(POST_C, 403, _newcomer("post", "/couple"), id="POST /couple 403"),
    pytest.param(
        POST_C, 401, _no_session("post", "/couple"), id="POST /couple 401", marks=_undeclared(NO401)
    ),
    pytest.param(
        POST_C,
        503,
        _no_bot("/couple"),
        id="POST /couple 503",
        marks=_undeclared("503 service_unavailable без имени бота — не объявлен"),
    ),
    pytest.param(INV, 201, _s_invite_201, id="POST /couple/invite 201"),
    pytest.param(INV, 409, _s_invite_409, id="POST /couple/invite 409"),
    pytest.param(INV, 403, _newcomer("post", "/couple/invite"), id="POST /couple/invite 403"),
    pytest.param(
        INV,
        404,
        _s_invite_404,
        id="POST /couple/invite 404",
        marks=_undeclared("POST /couple/invite без пары отдаёт 404 (FB007) — не объявлен"),
    ),
    pytest.param(
        INV,
        401,
        _no_session("post", "/couple/invite"),
        id="POST /couple/invite 401",
        marks=_undeclared(NO401),
    ),
    pytest.param(
        INV,
        503,
        _no_bot("/couple/invite"),
        id="POST /couple/invite 503",
        marks=_undeclared("503 service_unavailable без имени бота — не объявлен"),
    ),
    pytest.param(LEAVE, 204, _s_leave_204, id="POST /couple/leave 204"),
    pytest.param(LEAVE, 404, _s_leave_404, id="POST /couple/leave 404"),
    pytest.param(
        LEAVE,
        403,
        _newcomer("post", "/couple/leave"),
        id="POST /couple/leave 403",
        marks=_undeclared("POST /couple/leave отдаёт 403 onboarding_required — не объявлен"),
    ),
    pytest.param(
        LEAVE,
        401,
        _no_session("post", "/couple/leave"),
        id="POST /couple/leave 401",
        marks=_undeclared(NO401),
    ),
    pytest.param(VIEW, 200, _s_view_200, id="GET /invites 200"),
    pytest.param(VIEW, 404, _s_view_404, id="GET /invites 404"),
    pytest.param(VIEW, 410, _s_view_410, id="GET /invites 410"),
    pytest.param(
        VIEW,
        401,
        _no_session("get", "/invites/{t}"),
        id="GET /invites 401",
        marks=_undeclared(NO401),
    ),
    pytest.param(ACC, 200, _s_accept_200, id="POST accept 200"),
    pytest.param(ACC, 403, _newcomer("post", "/invites/{t}/accept"), id="POST accept 403"),
    pytest.param(ACC, 404, _s_accept_404, id="POST accept 404"),
    pytest.param(ACC, 409, _s_accept_409, id="POST accept 409"),
    pytest.param(ACC, 410, _s_accept_410, id="POST accept 410"),
    pytest.param(
        ACC,
        401,
        _no_session("post", "/invites/{t}/accept"),
        id="POST accept 401",
        marks=_undeclared(NO401),
    ),
]


@pytest.mark.parametrize(("op", "status", "scenario"), SERVED)
async def test_served_status_is_declared(
    api: httpx.AsyncClient,
    engine: AsyncEngine,
    op: tuple[str, str],
    status: int,
    scenario: Scenario,
) -> None:
    r = await scenario(api, engine)
    assert r.status_code == status, r.text  # статус действительно отдаётся сервером
    method, path = op
    assert str(status) in _declared(method, path), (method, path, status)
    if status >= 400:
        _err(r, status, r.json()["code"])
    elif (method, path) == VIEW:
        _preview(r)
    elif r.content:
        _ok(r, status, _success_schema(method, path))


def _success_schema(method: str, path: str) -> str:
    return {
        ("get", "/couple"): "Couple",
        ("post", "/couple"): "InviteCreated",
        ("post", "/couple/invite"): "InviteCreated",
        ("post", "/invites/{token}/accept"): "Couple",
    }.get((method, path), "Error")


@pytest.mark.xfail(
    strict=True,
    reason="D-5 (1.3-test, для A1, MINOR): коды not_found, own_invite, couple_not_pending "
    "отдаются, но не перечислены в Error.code.examples (там есть couple_not_active)",
)
def test_served_error_codes_listed_in_contract() -> None:
    known = set(_schema("Error")["properties"]["code"]["examples"])
    served = {
        "unauthorized",
        "onboarding_required",
        "not_found",
        "already_in_couple",
        "own_invite",
        "couple_not_pending",
        "invite_expired",
        "invite_used",
        "service_unavailable",
    }
    assert served <= known, served - known

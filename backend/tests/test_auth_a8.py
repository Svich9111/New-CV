"""1.1 / ФТ-01 — независимые тесты A8 (тестировщик).

initData собирается здесь по документации Telegram («Validating data received via the Mini App»)
без использования `sign_init_data` из кода проекта: secret_key = HMAC_SHA256("WebAppData",
bot_token), hash = hex(HMAC_SHA256(secret_key, data_check_string)), data_check_string — все поля,
кроме hash, «key=value» по алфавиту через "\\n" (значения — после URL-декодирования).
Данные — выдуманные (CLAUDE.md). Отчёт — docs/reviews/1.1-test.md.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import random
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

import httpx
import jwt
import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from pydantic import SecretStr
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.core.session import AUDIENCE
from app.core.telegram import InitDataError, validate_init_data
from app.main import app

BOT_TOKEN = "777000:A8-independent-fake-token"
SECRET = "a8" * 20
DAY = 86400
NOW = 1_800_000_000.0
SPEC_PATH = Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"


# --- независимая подпись по документации Telegram ---------------------------------------------


def tg_hash(fields: dict[str, str], bot_token: str = BOT_TOKEN) -> str:
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    return hmac.new(secret_key, dcs.encode("utf-8"), hashlib.sha256).hexdigest()


def tg_encode(
    fields: dict[str, str],
    digest: str,
    order: list[str] | None = None,
    quoter: Callable[[str], str] = lambda v: quote(v, safe=""),
) -> str:
    """Собрать строку initData, как её отдаёт клиент Telegram (hash — последним)."""
    keys = order or list(fields)
    parts = [f"{k}={quoter(fields[k])}" for k in keys]
    return "&".join([*parts, f"hash={digest}"])


def tg_user_json(tg_id: int, first_name: str = "Пётр", **extra: Any) -> str:
    # Telegram сериализует JSON компактно и экранирует "/" как "\/".
    raw = json.dumps(
        {"id": tg_id, "first_name": first_name, **extra},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return raw.replace("/", "\\/")


def real_like_fields(tg_id: int, auth_date: int, first_name: str = "Пётр") -> dict[str, str]:
    """Поля, как у современного клиента Telegram: есть signature (Ed25519, Bot API 7.x/8.0)."""
    return {
        "user": tg_user_json(
            tg_id,
            first_name,
            last_name="Тестов",
            username="a8_fake_user",
            language_code="ru",
            is_premium=True,
            allows_write_to_pm=True,
            photo_url="https://t.me/i/userpic/320/fake.svg",
        ),
        "chat_instance": "-4785637128459990000",
        "chat_type": "sender",
        "auth_date": str(auth_date),
        "signature": "Zm9yZ2VkLWJ1dC1zaWduZWQtYnktaGFzaA_-fakeEd25519SignatureValueAAAA",
    }


def signed(fields: dict[str, str], **kw: Any) -> str:
    return tg_encode(fields, tg_hash(fields), **kw)


# --- уровень функции проверки (без БД) --------------------------------------------------------


def test_independent_signature_with_signature_field_accepted() -> None:
    fields = real_like_fields(424242, int(NOW) - 5)
    user = validate_init_data(signed(fields), BOT_TOKEN, DAY, now=NOW)
    assert user.id == 424242


def test_signature_field_is_part_of_data_check_string() -> None:
    """signature подписан hash'ем: подмена signature должна ломать проверку."""
    fields = real_like_fields(424242, int(NOW) - 5)
    digest = tg_hash(fields)
    tampered = {**fields, "signature": fields["signature"][:-4] + "BBBB"}
    with pytest.raises(InitDataError):
        validate_init_data(tg_encode(tampered, digest), BOT_TOKEN, DAY, now=NOW)


def test_removed_field_rejected() -> None:
    fields = real_like_fields(424242, int(NOW) - 5)
    digest = tg_hash(fields)
    without = {k: v for k, v in fields.items() if k != "chat_instance"}
    with pytest.raises(InitDataError):
        validate_init_data(tg_encode(without, digest), BOT_TOKEN, DAY, now=NOW)


@pytest.mark.parametrize(
    "first_name",
    ["Анна-Мария", "Zoë 😊 李", "a+b c%20d&e=f", 'Иван \\/ "кавычки" \' \t', "‮RTL"],
)
def test_unicode_and_special_chars_in_first_name(first_name: str) -> None:
    fields = real_like_fields(1001, int(NOW) - 1, first_name=first_name)
    assert validate_init_data(signed(fields), BOT_TOKEN, DAY, now=NOW).id == 1001


@pytest.mark.parametrize("seed", range(5))
def test_field_order_in_query_does_not_matter(seed: int) -> None:
    fields = real_like_fields(1002, int(NOW) - 1)
    order = list(fields)
    random.Random(seed).shuffle(order)  # noqa: S311 — детерминированный порядок, не крипто
    digest = tg_hash(fields)
    keys_with_hash = [*order]
    parts = [f"{k}={quote(fields[k], safe='')}" for k in keys_with_hash]
    # hash в середине строки — тоже допустимо
    parts.insert(seed % (len(parts) + 1), f"hash={digest}")
    assert validate_init_data("&".join(parts), BOT_TOKEN, DAY, now=NOW).id == 1002


@pytest.mark.parametrize(
    "quoter",
    [lambda v: quote(v, safe=""), quote_plus],
    ids=["percent-%20", "plus-for-space"],
)
def test_url_encoding_space_as_plus_or_percent20(quoter: Callable[[str], str]) -> None:
    fields = real_like_fields(1003, int(NOW) - 1, first_name="Мария Петровна + Ко")
    data = signed(fields, quoter=quoter)
    assert validate_init_data(data, BOT_TOKEN, DAY, now=NOW).id == 1003


def test_unencoded_plus_changes_meaning_and_is_rejected() -> None:
    """Литеральный «+» без кодирования декодируется как пробел — подпись не совпадает."""
    fields = real_like_fields(1004, int(NOW) - 1, first_name="a+b")
    data = signed(fields, quoter=lambda v: quote(v, safe="+"))
    with pytest.raises(InitDataError):
        validate_init_data(data, BOT_TOKEN, DAY, now=NOW)


def test_uppercase_hash_accepted() -> None:
    """hex регистронезависим; сервер приводит hash к нижнему регистру."""
    fields = real_like_fields(1005, int(NOW) - 1)
    data = tg_encode(fields, tg_hash(fields).upper())
    assert validate_init_data(data, BOT_TOKEN, DAY, now=NOW).id == 1005


@pytest.mark.parametrize(
    "mangle",
    [
        lambda h: h[:-1],
        lambda h: h + "0",
        lambda h: " " + h,
        lambda h: h[:-1] + ("0" if h[-1] != "0" else "1"),
        lambda h: "",
        lambda h: "z" * 64,
    ],
    ids=["short", "long", "space", "one-char", "empty", "non-hex"],
)
def test_mangled_hash_rejected(mangle: Callable[[str], str]) -> None:
    fields = real_like_fields(1006, int(NOW) - 1)
    with pytest.raises(InitDataError):
        validate_init_data(tg_encode(fields, mangle(tg_hash(fields))), BOT_TOKEN, DAY, now=NOW)


def test_hash_of_other_bot_rejected() -> None:
    fields = real_like_fields(1007, int(NOW) - 1)
    data = tg_encode(fields, tg_hash(fields, "111:other-bot"))
    with pytest.raises(InitDataError):
        validate_init_data(data, BOT_TOKEN, DAY, now=NOW)


def test_hash_with_bot_token_as_key_directly_rejected() -> None:
    """Частая ошибка: HMAC(bot_token, dcs) вместо HMAC(HMAC("WebAppData", token), dcs)."""
    fields = real_like_fields(1008, int(NOW) - 1)
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    wrong = hmac.new(BOT_TOKEN.encode(), dcs.encode(), hashlib.sha256).hexdigest()
    with pytest.raises(InitDataError):
        validate_init_data(tg_encode(fields, wrong), BOT_TOKEN, DAY, now=NOW)


@pytest.mark.parametrize(
    ("age", "ok"),
    [(DAY - 1, True), (DAY, True), (DAY + 1, False), (-30, True), (-61, False)],
)
def test_age_boundaries(age: int, ok: bool) -> None:
    fields = real_like_fields(1009, int(NOW) - age)
    data = signed(fields)
    if ok:
        assert validate_init_data(data, BOT_TOKEN, DAY, now=NOW).id == 1009
    else:
        with pytest.raises(InitDataError):
            validate_init_data(data, BOT_TOKEN, DAY, now=NOW)


def test_duplicate_hash_key_rejected() -> None:
    fields = real_like_fields(1010, int(NOW) - 1)
    data = signed(fields) + "&hash=" + "0" * 64
    with pytest.raises(InitDataError):
        validate_init_data(data, BOT_TOKEN, DAY, now=NOW)


# --- HTTP-уровень ------------------------------------------------------------------------------


def _settings(**over: Any) -> Callable[[], Settings]:
    base: dict[str, Any] = {
        "telegram_bot_token": SecretStr(BOT_TOKEN),
        "session_secret": SecretStr(SECRET),
        "session_ttl_s": 900,
    }
    base.update(over)
    return lambda: Settings(**base)


@pytest.fixture
async def make_client(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Callable[..., httpx.AsyncClient]]:
    app.state.sessionmaker = sessionmaker
    clients: list[httpx.AsyncClient] = []

    def factory(**settings_over: Any) -> httpx.AsyncClient:
        app.dependency_overrides[get_settings] = _settings(**settings_over)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        c = httpx.AsyncClient(transport=transport, base_url="http://test")
        clients.append(c)
        return c

    yield factory
    for c in clients:
        await c.aclose()
    app.dependency_overrides.clear()


@pytest.fixture
async def client(make_client: Callable[..., httpx.AsyncClient]) -> httpx.AsyncClient:
    return make_client()


def _tg_id() -> int:
    return 7_000_000_000 + uuid.uuid4().int % 1_000_000_000


def _now_init_data(tg_id: int, first_name: str = "Пётр") -> str:
    return signed(real_like_fields(tg_id, int(time.time()) - 2, first_name=first_name))


async def _count_users(engine: AsyncEngine, tg_id: int) -> int:
    async with engine.connect() as conn:
        res = await conn.execute(
            text("SELECT count(*) FROM users WHERE telegram_id = :t"), {"t": tg_id}
        )
        return int(res.scalar_one())


# Валидация ответов JSON-схемами контракта.


def _spec() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    return data


def _validator(schema_name: str) -> Draft202012Validator:
    spec = _spec()
    registry: Registry[Any] = Registry().with_resource(
        "urn:openapi", Resource.from_contents(spec, default_specification=DRAFT202012)
    )
    return Draft202012Validator(
        {"$ref": f"urn:openapi#/components/schemas/{schema_name}"},
        registry=registry,
        format_checker=FormatChecker(),
    )


def assert_schema(body: Any, schema_name: str) -> None:
    errors = list(_validator(schema_name).iter_errors(body))
    assert not errors, [e.message for e in errors]
    # Строже контракта: нет полей, которых контракт не объявляет.
    props = set(_spec()["components"]["schemas"][schema_name]["properties"])
    assert set(body) <= props, set(body) - props


async def test_login_with_independent_real_like_init_data(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/telegram", json={"init_data": _now_init_data(_tg_id())})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert_schema(body, "Session")
    assert_schema(body["me"], "Me")
    me = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert_schema(me.json(), "Me")
    assert me.json() == body["me"]


async def test_session_token_claims(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/telegram", json={"init_data": _now_init_data(_tg_id())})
    token = resp.json()["access_token"]
    claims = jwt.decode(token, SECRET, algorithms=["HS256"], audience=AUDIENCE)
    assert claims["sub"] == resp.json()["me"]["id"]
    assert claims["exp"] - claims["iat"] == 900 == resp.json()["expires_in"]
    # В токене нет telegram_id и имени (ТЗ §9.1 п.2).
    assert set(claims) == {"sub", "iat", "exp", "aud"}


@pytest.mark.parametrize(
    "case",
    ["forged", "tampered", "added", "duplicate", "no_hash", "expired", "future", "garbage"],
)
async def test_rejections_are_error_schema_and_create_no_user(
    client: httpx.AsyncClient, engine: AsyncEngine, case: str
) -> None:
    tg = _tg_id()
    now = int(time.time())
    fields = real_like_fields(tg, now - 2)
    good = signed(fields)
    data = {
        "forged": tg_encode(fields, tg_hash(fields, "1:forged")),
        "tampered": good.replace(quote(f'"id":{tg}', safe=""), quote(f'"id":{tg + 1}', safe="")),
        "added": good + "&start_param=evil",
        "duplicate": good + f"&auth_date={now}",
        "no_hash": good.rsplit("&hash=", 1)[0],
        "expired": signed(real_like_fields(tg, now - DAY - 120)),
        "future": signed(real_like_fields(tg, now + 3600)),
        "garbage": "%%%&&==",
    }[case]
    if case == "tampered":
        assert data != good
    resp = await client.post("/api/v1/auth/telegram", json={"init_data": data})
    assert resp.status_code == 401
    assert resp.json() == {
        "code": "invalid_init_data",
        "message": "Не удалось подтвердить вход через Telegram",
    }
    assert_schema(resp.json(), "Error")
    assert data not in resp.text
    assert await _count_users(engine, tg) == 0
    assert await _count_users(engine, tg + 1) == 0


async def test_empty_bot_token_http_refuses(
    make_client: Callable[..., httpx.AsyncClient], engine: AsyncEngine
) -> None:
    c = make_client(telegram_bot_token=SecretStr(""))
    tg = _tg_id()
    fields = real_like_fields(tg, int(time.time()) - 2)
    # initData подписан «пустым токеном» — подпись сошлась бы, если бы проверка токена не стояла.
    resp = await c.post(
        "/api/v1/auth/telegram", json={"init_data": tg_encode(fields, tg_hash(fields, ""))}
    )
    # Контракт: 503 service_unavailable (сервис не настроен), а не вход и не 500.
    assert resp.status_code == 503
    assert resp.headers.get("content-type", "").startswith("application/json")
    assert resp.json()["code"] == "service_unavailable"
    assert_schema(resp.json(), "Error")
    assert "access_token" not in resp.text
    assert await _count_users(engine, tg) == 0


async def test_name_from_telegram_not_stored(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    tg = _tg_id()
    resp = await client.post(
        "/api/v1/auth/telegram", json={"init_data": _now_init_data(tg, "Уникальное Имя A8")}
    )
    assert resp.status_code == 200
    assert resp.json()["me"]["name"] is None
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT to_jsonb(u) AS j FROM users u WHERE telegram_id = :t"), {"t": tg}
            )
        ).one()
    stored = json.dumps(row.j, ensure_ascii=False)
    assert row.j["name"] is None and row.j["address_form"] is None
    for leaked in ("Уникальное Имя A8", "Тестов", "a8_fake_user", "userpic"):
        assert leaked not in stored


async def test_concurrent_first_login_creates_one_user(
    make_client: Callable[..., httpx.AsyncClient], engine: AsyncEngine
) -> None:
    c = make_client()
    tg = _tg_id()
    payloads = [_now_init_data(tg) for _ in range(6)]
    resps = await asyncio.gather(
        *(c.post("/api/v1/auth/telegram", json={"init_data": p}) for p in payloads)
    )
    assert [r.status_code for r in resps] == [200] * 6, [r.text for r in resps]
    assert len({r.json()["me"]["id"] for r in resps}) == 1
    assert await _count_users(engine, tg) == 1


async def test_long_valid_init_data_accepted(client: httpx.AsyncClient) -> None:
    """Реальный initData — сотни байт; с длинными полями (до ~3–4 КБ) вход должен работать."""
    data = _now_init_data(_tg_id(), first_name="Я" * 600)
    assert 3000 < len(data) < 8192
    resp = await client.post("/api/v1/auth/telegram", json={"init_data": data})
    assert resp.status_code == 200


@pytest.mark.parametrize("size", [8193, 200_000, 2_000_000])
async def test_very_long_init_data_rejected_without_echo(
    client: httpx.AsyncClient, size: int
) -> None:
    junk = "user=" + "A" * (size - 5)
    started = time.monotonic()
    resp = await client.post("/api/v1/auth/telegram", json={"init_data": junk})
    assert time.monotonic() - started < 5
    assert resp.status_code in (401, 413, 422)
    assert_schema(resp.json(), "Error")
    assert "AAAAAAAAAA" not in resp.text


async def test_no_init_data_or_token_in_logs(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    tg = _tg_id()
    data = _now_init_data(tg, first_name="ЛогНеДолженВидеть")
    resp = await client.post("/api/v1/auth/telegram", json={"init_data": data})
    token = resp.json()["access_token"]
    await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    bad = data.rsplit("&hash=", 1)[0] + "&hash=" + "0" * 64
    await client.post("/api/v1/auth/telegram", json={"init_data": bad})
    text_all = caplog.text + "".join(str(r.args) for r in caplog.records)
    digest = data.rsplit("&hash=", 1)[1]
    for secret in (data, digest, token, "ЛогНеДолженВидеть", BOT_TOKEN, SECRET, "0" * 64):
        assert secret not in text_all


@pytest.mark.parametrize(
    "header",
    ["Basic Zm9vOmJhcg==", "Bearer", "Bearer ", "bearer x.y.z", "Token abc"],
)
async def test_me_bad_authorization_header(client: httpx.AsyncClient, header: str) -> None:
    resp = await client.get("/api/v1/me", headers={"Authorization": header})
    assert resp.status_code == 401
    assert_schema(resp.json(), "Error")


async def test_token_without_aud_or_with_other_alg_rejected(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/telegram", json={"init_data": _now_init_data(_tg_id())})
    uid = resp.json()["me"]["id"]
    now = int(time.time())
    no_aud = jwt.encode({"sub": uid, "iat": now, "exp": now + 600}, SECRET, algorithm="HS256")
    other_aud = jwt.encode(
        {"sub": uid, "iat": now, "exp": now + 600, "aud": "x"}, SECRET, algorithm="HS256"
    )
    hs512 = jwt.encode(
        {"sub": uid, "iat": now, "exp": now + 600, "aud": AUDIENCE}, SECRET, algorithm="HS512"
    )
    for tok in (no_aud, other_aud, hs512):
        r = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 401


async def test_empty_session_secret_issues_no_token(
    make_client: Callable[..., httpx.AsyncClient],
) -> None:
    """Пустой FEELINGS_SESSION_SECRET: токен не выдаётся ни при каких условиях."""
    c = make_client(session_secret=SecretStr(""))
    resp = await c.post("/api/v1/auth/telegram", json={"init_data": _now_init_data(_tg_id())})
    assert resp.status_code != 200
    assert "access_token" not in resp.text


@pytest.mark.parametrize("weak", ["", "short", "x" * 31])
async def test_empty_session_secret_controlled_refusal(
    make_client: Callable[..., httpx.AsyncClient], engine: AsyncEngine, weak: str
) -> None:
    """Пустой или слабый секрет (D-1): отказ в формате контракта (503 service_unavailable),
    без необработанного исключения, и пользователь не создаётся (критерий «При отказе
    пользователь в БД не создаётся»)."""
    c = make_client(session_secret=SecretStr(weak))
    tg = _tg_id()
    resp = await c.post("/api/v1/auth/telegram", json={"init_data": _now_init_data(tg)})
    assert resp.status_code == 503
    assert resp.headers.get("content-type", "").startswith("application/json")
    assert resp.json()["code"] == "service_unavailable"
    assert_schema(resp.json(), "Error")
    assert "access_token" not in resp.text
    assert await _count_users(engine, tg) == 0


async def test_secret_of_exactly_32_chars_is_enough(
    make_client: Callable[..., httpx.AsyncClient],
) -> None:
    """Граница MIN_SECRET_LEN: 32 символа — вход работает, токен принимается."""
    c = make_client(session_secret=SecretStr("y" * 32))
    resp = await c.post("/api/v1/auth/telegram", json={"init_data": _now_init_data(_tg_id())})
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    me = await c.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200


async def test_empty_session_secret_rejects_token_forged_with_empty_key(
    make_client: Callable[..., httpx.AsyncClient],
) -> None:
    import base64

    def b64(x: bytes) -> bytes:
        return base64.urlsafe_b64encode(x).rstrip(b"=")

    now = int(time.time())
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(
        json.dumps(
            {"sub": str(uuid.uuid4()), "iat": now, "exp": now + 600, "aud": AUDIENCE}
        ).encode()
    )
    sig = b64(hmac.new(b"", head + b"." + body, hashlib.sha256).digest())
    token = (head + b"." + body + b"." + sig).decode()
    c = make_client(session_secret=SecretStr(""))
    resp = await c.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


async def test_weak_session_secret_rejects_tokens(
    make_client: Callable[..., httpx.AsyncClient],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Секрет короче 32 символов: выпуск токена запрещён (issue_token), значит и приём токенов
    с таким ключом должен быть запрещён — иначе сессию можно подобрать перебором."""
    from app.repositories import users

    uid = await users.upsert_by_telegram_id(sessionmaker, _tg_id())
    assert uid is not None
    now = int(time.time())
    weak = "abc"
    token = jwt.encode(
        {"sub": str(uid), "iat": now, "exp": now + 600, "aud": AUDIENCE}, weak, algorithm="HS256"
    )
    c = make_client(session_secret=SecretStr(weak))
    resp = await c.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


async def test_protected_endpoint_listed_in_app_requires_session(
    client: httpx.AsyncClient,
) -> None:
    """Все маршруты приложения, кроме входа, health и документации, требуют сессию."""
    public = {"/api/v1/auth/telegram", "/api/v1/health"}
    checked = 0
    for path, ops in app.openapi()["paths"].items():
        if path in public or "{" in path:
            continue
        for m in ops:
            r = await client.request(m.upper(), path)
            assert r.status_code == 401, (m, path, r.status_code)
            checked += 1
    assert checked >= 1


@pytest.mark.parametrize("where", ["auth_date", "hash_first_bad"])
def test_duplicate_key_rejected_even_when_signature_would_match(where: str) -> None:
    """Дубликат, при котором «последнее значение» даёт верную подпись, всё равно отклоняется
    (иначе разные парсеры initData — клиент, прокси, сервер — увидят разные данные)."""
    fields = real_like_fields(1011, int(NOW) - 1)
    good = signed(fields)
    if where == "auth_date":
        data = f"auth_date={fields['auth_date']}&" + good
    else:
        data = "hash=" + "0" * 64 + "&" + good
    with pytest.raises(InitDataError):
        validate_init_data(data, BOT_TOKEN, DAY, now=NOW)


# --- раунд 2: удалённый аккаунт, проверка конфигурации при старте, контракт кодов -------------


async def _delete_account(engine: AsyncEngine, tg_id: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET deleted_at = now() WHERE telegram_id = :t"), {"t": tg_id}
        )


async def test_current_user_dependency_itself_rejects_deleted_user(
    sessionmaker: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    client: httpx.AsyncClient,
) -> None:
    """Проверка удалённого пользователя — в самой зависимости CurrentUser, а не только в /me:
    будущий эндпоинт, который сам не читает users, тоже не пускает удалённого (MINOR-6)."""
    from fastapi import FastAPI

    from app.api.deps import CurrentUser
    from app.errors import install_error_handlers

    probe = FastAPI()
    probe.state.sessionmaker = sessionmaker
    install_error_handlers(probe)

    @probe.get("/probe")
    async def _probe(uid: CurrentUser) -> dict[str, str]:
        return {"uid": str(uid)}

    probe.dependency_overrides[get_settings] = _settings()
    tg = _tg_id()
    login = await client.post("/api/v1/auth/telegram", json={"init_data": _now_init_data(tg)})
    token = login.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=probe, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as pc:
        ok = await pc.get("/probe", headers=auth)
        assert ok.status_code == 200
        assert ok.json() == {"uid": login.json()["me"]["id"]}
        await _delete_account(engine, tg)
        gone = await pc.get("/probe", headers=auth)
    assert gone.status_code == 401
    assert gone.json()["code"] == "unauthorized"
    assert_schema(gone.json(), "Error")


async def test_deleted_account_login_is_403_and_creates_nothing(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    tg = _tg_id()
    first = await client.post("/api/v1/auth/telegram", json={"init_data": _now_init_data(tg)})
    assert first.status_code == 200
    await _delete_account(engine, tg)
    resp = await client.post("/api/v1/auth/telegram", json={"init_data": _now_init_data(tg)})
    assert resp.status_code == 403
    assert resp.json()["code"] == "account_deleted"
    assert_schema(resp.json(), "Error")
    assert "access_token" not in resp.text
    assert await _count_users(engine, tg) == 1
    me = await client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {first.json()['access_token']}"}
    )
    assert me.status_code == 401


async def test_misconfig_refusal_precedes_any_parsing(
    make_client: Callable[..., httpx.AsyncClient],
) -> None:
    """Без токена бота ответ 503 одинаков для любого initData (не раскрывает, верна ли подпись)."""
    c = make_client(telegram_bot_token=SecretStr(""))
    for data in ("%%%&&==", _now_init_data(_tg_id())):
        resp = await c.post("/api/v1/auth/telegram", json={"init_data": data})
        assert resp.status_code == 503
        assert resp.json() == {"code": "service_unavailable", "message": "Вход временно недоступен"}


@pytest.mark.parametrize(
    ("bot_token", "secret"),
    [("", SECRET), (BOT_TOKEN, ""), (BOT_TOKEN, "z" * 31)],
)
async def test_service_does_not_start_with_bad_keys(
    monkeypatch: pytest.MonkeyPatch, bot_token: str, secret: str
) -> None:
    """check_auth_config в lifespan: сервис не стартует с пустым токеном или слабым секретом."""
    from app.config import ConfigError
    from app.main import lifespan

    monkeypatch.setenv("FEELINGS_TELEGRAM_BOT_TOKEN", bot_token)
    monkeypatch.setenv("FEELINGS_SESSION_SECRET", secret)
    saved = app.state.sessionmaker
    app.state.sessionmaker = None
    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigError):
            async with lifespan(app):
                pytest.fail("сервис запустился с неверной конфигурацией ключей")
        assert app.state.sessionmaker is None
    finally:
        get_settings.cache_clear()
        app.state.sessionmaker = saved


async def test_service_starts_with_good_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import lifespan

    monkeypatch.setenv("FEELINGS_TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("FEELINGS_SESSION_SECRET", SECRET)
    saved = app.state.sessionmaker
    app.state.sessionmaker = None
    get_settings.cache_clear()
    try:
        async with lifespan(app):
            assert app.state.sessionmaker is not None
    finally:
        get_settings.cache_clear()
        app.state.sessionmaker = saved


async def test_every_auth_status_is_declared_in_contract(
    make_client: Callable[..., httpx.AsyncClient], engine: AsyncEngine
) -> None:
    """Каждый код, который реально отдаёт вход, объявлен в api/openapi.yaml (MAJOR-3 / D-3)."""
    declared = set(_spec()["paths"]["/auth/telegram"]["post"]["responses"])
    c = make_client()
    tg = _tg_id()
    seen = {
        (await c.post("/api/v1/auth/telegram", json={"init_data": _now_init_data(tg)})).status_code
    }
    seen.add((await c.post("/api/v1/auth/telegram", json={"init_data": "x=1"})).status_code)
    seen.add((await c.post("/api/v1/auth/telegram", json={"init_data": ""})).status_code)
    await _delete_account(engine, tg)
    seen.add(
        (await c.post("/api/v1/auth/telegram", json={"init_data": _now_init_data(tg)})).status_code
    )
    broken = make_client(session_secret=SecretStr(""))
    seen.add(
        (
            await broken.post("/api/v1/auth/telegram", json={"init_data": _now_init_data(_tg_id())})
        ).status_code
    )
    assert seen == {200, 401, 403, 422, 503}
    assert {str(s) for s in seen} <= declared


@pytest.mark.parametrize(
    "bad_hash",
    [
        "%D0%B0",  # кириллица
        "%D0%B0" * 64,  # 64 символа, но не ASCII
        "%EF%BC%90" * 64,  # полноширинные цифры: isdigit() == True, но не hex
        "%00" * 64,
        "a" * 63 + "%E2%80%8B",  # zero-width space в конце
    ],
)
async def test_non_ascii_hash_is_401_contract_not_500(
    client: httpx.AsyncClient, engine: AsyncEngine, bad_hash: str
) -> None:
    """MAJOR-2: hash не из ASCII — штатный отказ 401 invalid_init_data, а не 500."""
    tg = _tg_id()
    base = _now_init_data(tg).rsplit("&hash=", 1)[0]
    resp = await client.post("/api/v1/auth/telegram", json={"init_data": f"{base}&hash={bad_hash}"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_init_data"
    assert_schema(resp.json(), "Error")
    assert await _count_users(engine, tg) == 0

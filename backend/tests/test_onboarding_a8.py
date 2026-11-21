"""1.2 / ФТ-02 — независимые тесты A8 (тестировщик): онбординг, согласия, 18+, OnboardedUser.

Карточка docs/tasks/1.2.md; ТЗ §4.1 п.2, ФТ-02, §7 (users, agents, safety_events), §9.1 п.2 и
п.5, §9.3 («младше 18 — доступ не предоставляется», событие без текста). Вход — независимая
подпись initData из tests/test_auth_a8.py (не `sign_init_data` проекта). Ответы проверяются
JSON-схемой контракта строже самого контракта (без необъявленных полей). Данные — выдуманные.
Отчёт — docs/reviews/1.2-test.md.
"""

import asyncio
import datetime as dt
import logging
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
from tests.test_auth_a8 import _now_init_data, _settings, _tg_id, assert_schema

URL = "/api/v1/me/onboarding"
ME = "/api/v1/me"
MARK = "A8-утечка-c41d"
CONSENTS = ("consent_terms", "consent_data", "consent_sensitive")
MARKS = ("consent_terms_at", "consent_data_at", "consent_sensitive_at", "age_confirmed_at")
REPORT = "docs/reviews/1.2-test.md"

# Свой защищённый маршрут под OnboardedUser (отдельное приложение, в продукт не публикуется).
guarded = FastAPI()
install_error_handlers(guarded)


@guarded.get("/guarded")
async def _guarded(user_id: OnboardedUser) -> dict[str, str]:
    return {"owner": str(user_id)}


def body(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "Вера",
        "address_form": "на вы",
        "partner_name": "Степан",
        "timezone": "Asia/Yekaterinburg",
        "consent_terms": True,
        "consent_data": True,
        "consent_sensitive": True,
        "age_confirmed": True,
    }
    data.update(over)
    return data


@pytest.fixture
async def api(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[httpx.AsyncClient]:
    app.state.sessionmaker = sessionmaker
    app.dependency_overrides[get_settings] = _settings()
    # raise_app_exceptions=False: необработанная ошибка видна как 500, а не как исключение теста.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def gate(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[httpx.AsyncClient]:
    guarded.state.sessionmaker = sessionmaker
    guarded.dependency_overrides[get_settings] = _settings()
    transport = httpx.ASGITransport(app=guarded, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    guarded.dependency_overrides.clear()


async def login(api: httpx.AsyncClient) -> tuple[dict[str, str], uuid.UUID]:
    resp = await api.post("/api/v1/auth/telegram", json={"init_data": _now_init_data(_tg_id())})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, uuid.UUID(data["me"]["id"])


async def user_row(engine: AsyncEngine, uid: uuid.UUID) -> Any:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT name, address_form, partner_name, timezone, consent_terms_at, "
                    "consent_data_at, consent_sensitive_at, age_confirmed_at, "
                    "char_length(name) AS name_len FROM users WHERE id = :id"
                ),
                {"id": uid},
            )
        ).one()


async def scalar(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def agents(engine: AsyncEngine, uid: uuid.UUID) -> int:
    return int(await scalar(engine, "SELECT count(*) FROM agents WHERE user_id = :id", id=uid))


async def underage_events(engine: AsyncEngine, uid: uuid.UUID) -> int:
    return int(
        await scalar(
            engine,
            "SELECT count(*) FROM safety_events WHERE user_id = :id AND type = 'underage'",
            id=uid,
        )
    )


def assert_error(resp: httpx.Response, status: int, code: str) -> None:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    assert_schema(resp.json(), "Error")
    assert set(resp.json()) == {"code", "message"}
    assert resp.json()["code"] == code
    assert resp.json()["message"].strip()


def assert_pristine(row: Any) -> None:
    assert (row.name, row.address_form, row.partner_name) == (None, None, None)
    assert row.timezone == "Europe/Moscow"
    for mark in MARKS:
        assert getattr(row, mark) is None, mark


# --- Ответ строго по схеме контракта для каждого кода (карточка, критерий 6) ----------------


async def test_every_status_is_contract_shaped(
    api: httpx.AsyncClient, gate: httpx.AsyncClient
) -> None:
    headers, uid = await login(api)
    assert_error(await api.put(URL, json=body()), 401, "unauthorized")
    assert_error(await gate.get("/guarded", headers=headers), 403, "onboarding_required")
    assert_error(await api.put(URL, json=body(name=""), headers=headers), 422, "validation_error")
    assert_error(
        await api.put(URL, json=body(age_confirmed=False), headers=headers), 403, "underage"
    )
    ok = await api.put(URL, json=body(), headers=headers)
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("application/json")
    assert_schema(ok.json(), "Me")
    assert set(ok.json()) == {
        "id",
        "name",
        "address_form",
        "partner_name",
        "timezone",
        "onboarding_completed",
        "has_active_couple",
    }
    assert ok.json()["id"] == str(uid)
    assert ok.json()["onboarding_completed"] is True


def test_contract_declares_codes_used_by_endpoint() -> None:
    from tests.test_auth_a8 import _spec

    spec = _spec()
    op = spec["paths"]["/me/onboarding"]["put"]
    assert {"200", "403", "422"} <= set(op["responses"])
    codes = spec["components"]["schemas"]["Error"]["properties"]["code"]["examples"]
    assert {"underage", "onboarding_required", "validation_error", "unauthorized"} <= set(codes)
    req = spec["components"]["schemas"]["OnboardingRequest"]
    for consent in CONSENTS:
        assert req["properties"][consent]["const"] is True
        assert consent in req["required"]
    assert "age_confirmed" in req["required"]


# --- Согласия: const true (критерий 1) ------------------------------------------------------


@pytest.mark.parametrize("consent", CONSENTS)
@pytest.mark.parametrize("value", ["true", "True", "1", [True], {"v": True}, 0, 0.0, "", None])
async def test_consent_must_be_literal_true(
    api: httpx.AsyncClient, engine: AsyncEngine, consent: str, value: object
) -> None:
    headers, uid = await login(api)
    assert_error(
        await api.put(URL, json=body(**{consent: value}), headers=headers), 422, "validation_error"
    )
    assert_pristine(await user_row(engine, uid))
    assert await agents(engine, uid) == 0


@pytest.mark.xfail(
    strict=True,
    reason=f"дефект D-3 ({REPORT}): число 1 / 1.0 принимается как согласие (контракт: boolean)",
)
@pytest.mark.parametrize("consent", CONSENTS)
@pytest.mark.parametrize("value", [1, 1.0])
async def test_numeric_one_is_not_consent(
    api: httpx.AsyncClient, engine: AsyncEngine, consent: str, value: object
) -> None:
    """JSON Schema контракта: {type: boolean, const: true} — 1 не boolean (ФТ-02, §9.4)."""
    headers, uid = await login(api)
    assert_error(
        await api.put(URL, json=body(**{consent: value}), headers=headers), 422, "validation_error"
    )
    assert_pristine(await user_row(engine, uid))


def test_contract_itself_rejects_numeric_consent() -> None:
    """Сама схема контракта не принимает 1 как согласие — расхождение именно в коде."""
    from tests.test_auth_a8 import _validator

    v = _validator("OnboardingRequest")
    assert v.is_valid(body())
    for consent in CONSENTS:
        assert not v.is_valid(body(**{consent: 1}))
        assert not v.is_valid(body(**{consent: False}))
    assert not v.is_valid(body(age_confirmed=1))


async def test_all_consents_false_with_underage_is_422_without_event(
    api: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Порядок проверок из заметок исполнителя: схема (согласия) раньше возраста."""
    headers, uid = await login(api)
    over = {c: False for c in CONSENTS}
    resp = await api.put(URL, json=body(age_confirmed=False, **over), headers=headers)
    assert_error(resp, 422, "validation_error")
    assert await underage_events(engine, uid) == 0
    assert_pristine(await user_row(engine, uid))


@pytest.mark.parametrize("consent", CONSENTS)
async def test_repeat_with_false_consent_does_not_roll_back(
    api: httpx.AsyncClient, gate: httpx.AsyncClient, engine: AsyncEngine, consent: str
) -> None:
    """Повтор с false — 422; ранее данные согласия и профиль не меняются, доступ сохраняется."""
    headers, uid = await login(api)
    assert (await api.put(URL, json=body(), headers=headers)).status_code == 200
    before = await user_row(engine, uid)
    resp = await api.put(URL, json=body(name="Другая", **{consent: False}), headers=headers)
    assert_error(resp, 422, "validation_error")
    after = await user_row(engine, uid)
    assert after == before
    assert (await gate.get("/guarded", headers=headers)).status_code == 200
    assert (await api.get(ME, headers=headers)).json()["onboarding_completed"] is True


# --- 18+ (критерий 2, ТЗ §9.3) --------------------------------------------------------------


async def test_underage_event_row_has_only_service_values(
    api: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    headers, uid = await login(api)
    t0 = dt.datetime.now(dt.UTC)
    resp = await api.put(
        URL, json=body(age_confirmed=False, name=MARK, partner_name=MARK), headers=headers
    )
    assert_error(resp, 403, "underage")
    assert MARK not in resp.text
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text("SELECT to_jsonb(e) AS j FROM safety_events e WHERE user_id = :id"),
                    {"id": uid},
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {"id", "user_id", "type", "created_at"}
    assert row["user_id"] == str(uid)
    assert row["type"] == "underage"
    created = dt.datetime.fromisoformat(row["created_at"])
    assert t0 - dt.timedelta(seconds=5) <= created <= dt.datetime.now(dt.UTC)
    assert MARK not in str(row)


async def test_marker_from_rejected_requests_is_nowhere_in_db(
    api: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Ни underage, ни 422 не сохраняют присланный текст ни в одной таблице (ТЗ §9.1, §9.3)."""
    headers, _ = await login(api)
    await api.put(URL, json=body(age_confirmed=False, name=MARK), headers=headers)
    await api.put(URL, json=body(consent_data=False, address_form=MARK), headers=headers)
    await api.put(URL, json=body(partner_name=MARK + "я" * 64), headers=headers)
    async with engine.connect() as conn:
        tables = (
            await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        ).scalars()
        for table in list(tables):
            found = (
                await conn.execute(
                    text(f'SELECT count(*) FROM "{table}" t WHERE to_jsonb(t)::text LIKE :m'),
                    {"m": f"%{MARK}%"},
                )
            ).scalar_one()
            assert found == 0, table


async def test_retry_after_underage(
    api: httpx.AsyncClient, gate: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Фиксирует текущее поведение: после underage повтор с true открывает доступ, событие
    остаётся. Блокировки после заявленного возраста < 18 нет — вопрос оператору (см. отчёт)."""
    headers, uid = await login(api)
    assert_error(
        await api.put(URL, json=body(age_confirmed=False), headers=headers), 403, "underage"
    )
    assert_error(await gate.get("/guarded", headers=headers), 403, "onboarding_required")
    resp = await api.put(URL, json=body(), headers=headers)
    assert resp.status_code == 200
    assert await underage_events(engine, uid) == 1
    assert await agents(engine, uid) == 1
    assert (await gate.get("/guarded", headers=headers)).status_code == 200


async def test_underage_twice_and_after_onboarding_keeps_profile_closes_access(
    api: httpx.AsyncClient, gate: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    headers, uid = await login(api)
    assert (await api.put(URL, json=body(), headers=headers)).status_code == 200
    before = await user_row(engine, uid)
    for _ in range(2):
        resp = await api.put(URL, json=body(age_confirmed=False, name=MARK), headers=headers)
        assert_error(resp, 403, "underage")
    after = await user_row(engine, uid)
    assert after.name == before.name == "Вера"  # профиль из отказного запроса не сохранён
    for mark in CONSENTS:
        assert getattr(after, f"{mark}_at") == getattr(before, f"{mark}_at")
    assert after.age_confirmed_at is None
    assert await underage_events(engine, uid) == 2
    assert await agents(engine, uid) == 1
    assert_error(await gate.get("/guarded", headers=headers), 403, "onboarding_required")
    assert (await api.get(ME, headers=headers)).json()["onboarding_completed"] is False


async def test_underage_of_deleted_user_is_401_without_event(
    api: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    headers, uid = await login(api)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now() WHERE id = :id"), {"id": uid})
    resp = await api.put(URL, json=body(age_confirmed=False), headers=headers)
    assert_error(resp, 401, "unauthorized")
    assert await underage_events(engine, uid) == 0


async def test_underage_of_one_user_does_not_touch_another(
    api: httpx.AsyncClient, gate: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Владелец — только из сессии: чужой id в теле не переносит отказ на другого (§9.2)."""
    headers_a, uid_a = await login(api)
    headers_b, uid_b = await login(api)
    assert (await api.put(URL, json=body(), headers=headers_b)).status_code == 200
    resp = await api.put(
        URL, json=body(age_confirmed=False, id=str(uid_b), user_id=str(uid_b)), headers=headers_a
    )
    assert_error(resp, 403, "underage")
    assert await underage_events(engine, uid_a) == 1
    assert await underage_events(engine, uid_b) == 0
    assert (await user_row(engine, uid_b)).age_confirmed_at is not None
    assert (await gate.get("/guarded", headers=headers_b)).status_code == 200


# --- Успешный онбординг, идемпотентность, гонки (критерий 3) --------------------------------


async def test_repeat_keeps_all_four_consent_times_and_agent_id(
    api: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    headers, uid = await login(api)
    assert (await api.put(URL, json=body(), headers=headers)).status_code == 200
    first = await user_row(engine, uid)
    agent_id = await scalar(engine, "SELECT id FROM agents WHERE user_id = :id", id=uid)
    await asyncio.sleep(0.01)
    resp = await api.put(
        URL, json=body(name="Верочка", timezone="Europe/Kaliningrad"), headers=headers
    )
    assert resp.status_code == 200
    second = await user_row(engine, uid)
    for mark in MARKS:
        assert getattr(second, mark) == getattr(first, mark), mark
    assert (second.name, second.timezone) == ("Верочка", "Europe/Kaliningrad")
    assert await scalar(engine, "SELECT id FROM agents WHERE user_id = :id", id=uid) == agent_id


async def test_preexisting_agent_is_reused(api: httpx.AsyncClient, engine: AsyncEngine) -> None:
    """Агент, созданный раньше (другим путём), не дублируется и не заменяется."""
    headers, uid = await login(api)
    async with engine.begin() as conn:
        agent_id = (
            await conn.execute(
                text("INSERT INTO agents (user_id) VALUES (:id) RETURNING id"), {"id": uid}
            )
        ).scalar_one()
    assert (await api.put(URL, json=body(), headers=headers)).status_code == 200
    assert await agents(engine, uid) == 1
    assert await scalar(engine, "SELECT id FROM agents WHERE user_id = :id", id=uid) == agent_id


async def test_race_onboarding_with_agent_creation_and_different_bodies(
    api: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    headers, uid = await login(api)

    async def create_agent() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO agents (user_id) VALUES (:id) ON CONFLICT (user_id) DO NOTHING"),
                {"id": uid},
            )

    names = [f"Вера-{i}" for i in range(6)]
    results = await asyncio.gather(
        *(api.put(URL, json=body(name=n), headers=headers) for n in names),
        create_agent(),
        create_agent(),
    )
    statuses = [r.status_code for r in results if isinstance(r, httpx.Response)]
    assert statuses == [200] * len(names)
    assert await agents(engine, uid) == 1
    row = await user_row(engine, uid)
    assert row.name in names
    for mark in MARKS:
        assert getattr(row, mark) is not None


async def test_two_users_onboard_concurrently_each_get_own_agent(
    api: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    sessions = [await login(api) for _ in range(5)]
    results = await asyncio.gather(
        *(api.put(URL, json=body(name=f"П{i}"), headers=h) for i, (h, _) in enumerate(sessions))
    )
    assert [r.status_code for r in results] == [200] * 5
    for i, (_, uid) in enumerate(sessions):
        assert await agents(engine, uid) == 1
        assert (await user_row(engine, uid)).name == f"П{i}"


# --- OnboardedUser (критерий 4) -------------------------------------------------------------


@pytest.mark.parametrize("mark", MARKS)
async def test_each_mark_removed_closes_access_and_me_agrees(
    api: httpx.AsyncClient, gate: httpx.AsyncClient, engine: AsyncEngine, mark: str
) -> None:
    headers, uid = await login(api)
    assert (await api.put(URL, json=body(), headers=headers)).status_code == 200
    ok = await gate.get("/guarded", headers=headers)
    assert ok.status_code == 200 and ok.json() == {"owner": str(uid)}
    async with engine.begin() as conn:
        await conn.execute(text(f"UPDATE users SET {mark} = NULL WHERE id = :id"), {"id": uid})
    assert_error(await gate.get("/guarded", headers=headers), 403, "onboarding_required")
    assert (await api.get(ME, headers=headers)).json()["onboarding_completed"] is False
    # Повторный онбординг восстанавливает отметку и доступ.
    assert (await api.put(URL, json=body(), headers=headers)).status_code == 200
    assert getattr(await user_row(engine, uid), mark) is not None
    assert (await gate.get("/guarded", headers=headers)).status_code == 200


async def test_only_marks_missing_closes_access_profile_alone_is_not_enough(
    api: httpx.AsyncClient, gate: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Профиль и агент без отметок согласий доступа не дают."""
    headers, uid = await login(api)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE users SET name = 'Вера', address_form = 'ты', partner_name = 'С' WHERE id = :id"
            ),
            {"id": uid},
        )
        await conn.execute(text("INSERT INTO agents (user_id) VALUES (:id)"), {"id": uid})
    assert_error(await gate.get("/guarded", headers=headers), 403, "onboarding_required")


@pytest.mark.parametrize("auth", [None, "Bearer x.y.z", "Basic dTpw"])
async def test_guard_without_valid_session_is_401(
    gate: httpx.AsyncClient, auth: str | None
) -> None:
    headers = {"Authorization": auth} if auth else {}
    assert_error(await gate.get("/guarded", headers=headers), 401, "unauthorized")


# --- Валидация: unicode, пробелы, длина (критерий 5) ----------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "Анна-Мария",
        "Zoë O'Connor",
        "李小龙",
        "محمد",  # RTL
        "שרה",  # RTL
        "Аня 🌸",
        "👨‍👩‍👧‍👦",  # ZWJ-последовательность
        "е́ль",  # комбинирующий знак
        '"Кавычки" & <теги>',
        "🙂" * 64,  # 64 символа вне BMP: граница по символам, а не байтам/UTF-16
        "ё" * 64,
    ],
)
@pytest.mark.parametrize("field", ["name", "address_form", "partner_name"])
async def test_unicode_values_round_trip(
    api: httpx.AsyncClient, engine: AsyncEngine, field: str, value: str
) -> None:
    headers, uid = await login(api)
    resp = await api.put(URL, json=body(**{field: value}), headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()[field] == value
    assert (await api.get(ME, headers=headers)).json()[field] == value
    row = await user_row(engine, uid)
    assert getattr(row, field) == value


@pytest.mark.parametrize("value", ["🙂" * 65, "я" * 65, "👨‍👩‍👧‍👦" * 10])
async def test_over_64_code_points_rejected(api: httpx.AsyncClient, value: str) -> None:
    headers, _ = await login(api)
    resp = await api.put(URL, json=body(partner_name=value), headers=headers)
    assert_error(resp, 422, "validation_error")
    assert value not in resp.text


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("  Вера  ", "Вера"),
        (" Вера　", "Вера"),
        ("Вера Мария", "Вера Мария"),
        ("Вера  Мария", "Вера  Мария"),
        (" " * 100 + "Вера" + " " * 100, "Вера"),
        (" " + "я" * 64 + " ", "я" * 64),
    ],
)
async def test_whitespace_is_trimmed_before_length_check(
    api: httpx.AsyncClient, engine: AsyncEngine, raw: str, stored: str
) -> None:
    headers, uid = await login(api)
    resp = await api.put(URL, json=body(name=raw), headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == stored
    assert (await user_row(engine, uid)).name == stored


@pytest.mark.parametrize("value", [" ", "\t", "\n", "　 ", " \r\n "])
@pytest.mark.parametrize("field", ["name", "address_form", "partner_name"])
async def test_blank_values_rejected(api: httpx.AsyncClient, field: str, value: str) -> None:
    headers, _ = await login(api)
    assert_error(
        await api.put(URL, json=body(**{field: value}), headers=headers), 422, "validation_error"
    )


@pytest.mark.parametrize("value", ["Ве\x00ра", "Ве\x1bра", "Ве\x7fра", "Ве\tра", "Ве\rра"])
async def test_c0_controls_inside_rejected(api: httpx.AsyncClient, value: str) -> None:
    headers, _ = await login(api)
    assert_error(
        await api.put(URL, json=body(name=value), headers=headers), 422, "validation_error"
    )


@pytest.mark.xfail(
    strict=True,
    reason=f"дефект D-1 ({REPORT}): C1-управляющие и разделители строк внутри имени принимаются",
)
@pytest.mark.parametrize("value", ["Ве\x85ра", "Ве\x9bра", "Ве ра", "Ве ра"])
async def test_c1_controls_and_line_separators_inside_rejected(
    api: httpx.AsyncClient, value: str
) -> None:
    headers, _ = await login(api)
    assert_error(
        await api.put(URL, json=body(name=value), headers=headers), 422, "validation_error"
    )


@pytest.mark.xfail(
    strict=True,
    reason=f"дефект D-2 ({REPORT}): имя из одних невидимых символов (Cf) не считается пустым",
)
@pytest.mark.parametrize("value", ["​", "﻿", "‍‍", "‮"])
async def test_invisible_only_value_rejected(api: httpx.AsyncClient, value: str) -> None:
    headers, _ = await login(api)
    assert_error(
        await api.put(URL, json=body(name=value), headers=headers), 422, "validation_error"
    )


async def test_lone_surrogate_is_422_not_500(api: httpx.AsyncClient, engine: AsyncEngine) -> None:
    headers, uid = await login(api)
    raw = (
        '{"name":"\\ud800","address_form":"ты","partner_name":"С","consent_terms":true,'
        '"consent_data":true,"consent_sensitive":true,"age_confirmed":true}'
    )
    resp = await api.put(
        URL, content=raw.encode(), headers={**headers, "Content-Type": "application/json"}
    )
    assert_error(resp, 422, "validation_error")
    assert_pristine(await user_row(engine, uid))


async def test_form_encoded_body_is_422(api: httpx.AsyncClient) -> None:
    headers, _ = await login(api)
    resp = await api.put(URL, data={"name": "Вера", "age_confirmed": "true"}, headers=headers)
    assert_error(resp, 422, "validation_error")


# --- Часовой пояс ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tz",
    [
        "UTC",
        "Etc/UTC",
        "Etc/GMT-3",
        "America/Argentina/Buenos_Aires",
        "Asia/Kolkata",
        "Asia/Kamchatka",
        "Europe/Kiev",  # ссылка из backward
        "Europe/Kyiv",
        "Pacific/Chatham",
    ],
)
async def test_valid_iana_zones_accepted(api: httpx.AsyncClient, tz: str) -> None:
    headers, _ = await login(api)
    resp = await api.put(URL, json=body(timezone=tz), headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["timezone"] == tz


@pytest.mark.parametrize(
    "tz",
    [
        "europe/moscow",
        "EUROPE/MOSCOW",
        "Europe/moscow",
        "utc",
        " Europe/Moscow",
        "Europe/Moscow ",
        "Europe//Moscow",
        "/Europe/Moscow",
        "Europe/",
        "Europe\\Moscow",
        "Europe/Moscow\x00",
        "posixrules",
        "Factory",
        "localtime",
        "posix/Europe/Moscow",
        "right/UTC",
        "zone.tab",
        "zone1970.tab",
        "tzdata.zi",
        "leapseconds",
        "iso3166.tab",
        "+03:00",
        "UTC+3",
        "MSK",
        "Москва",
        "/usr/share/zoneinfo/UTC",
        "Europe/" + MARK,
        "Europe/Moscow" * 10,
        42,
        True,
        ["UTC"],
    ],
)
async def test_invalid_zones_rejected(
    api: httpx.AsyncClient, engine: AsyncEngine, tz: object
) -> None:
    headers, uid = await login(api)
    resp = await api.put(URL, json=body(timezone=tz), headers=headers)
    assert_error(resp, 422, "validation_error")
    assert MARK not in resp.text
    assert_pristine(await user_row(engine, uid))


# --- Тело запроса не утекает (критерий 6, ТЗ §9.1 п.5) --------------------------------------


async def test_body_not_in_logs(
    api: httpx.AsyncClient, gate: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    headers, _ = await login(api)
    secrets = {
        "ok": MARK + "-ok",
        "underage": MARK + "-u18",
        "invalid": MARK + "-422" + "я" * 64,
        "consent": MARK + "-consent",
    }
    await api.put(URL, json=body(name=secrets["ok"]), headers=headers)
    await api.put(URL, json=body(name=secrets["underage"], age_confirmed=False), headers=headers)
    await api.put(URL, json=body(partner_name=secrets["invalid"]), headers=headers)
    await api.put(
        URL, json=body(address_form=secrets["consent"], consent_terms=False), headers=headers
    )
    await gate.get("/guarded", headers=headers)
    logged = caplog.text + "".join(f"{r.msg}{r.args}" for r in caplog.records)
    assert MARK not in logged


@pytest.mark.parametrize(
    "over",
    [
        {"name": MARK + "я" * 64},
        {"address_form": MARK + "\x00"},
        {"timezone": MARK},
        {"consent_sensitive": MARK},
        {"age_confirmed": MARK},
    ],
)
async def test_error_never_echoes_body(api: httpx.AsyncClient, over: dict[str, Any]) -> None:
    headers, _ = await login(api)
    resp = await api.put(URL, json=body(**over), headers=headers)
    assert_error(resp, 422, "validation_error")
    assert MARK not in resp.text
    assert "input" not in resp.text and "loc" not in resp.json()

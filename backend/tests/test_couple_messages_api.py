"""3.3 (A2): лента пары, оригинал по запросу, «прочитано» — `GET /couple/messages`,
`GET /couple/messages/{id}/original`, `POST /couple/messages/{id}/read`.

ФТ-10, ФТ-11; ТЗ §2 п.3, §4.4 п.3–4, §6.2 п.7, §7; DECISIONS.md АР-11, АР-24, АР-25. Сообщения
отправляются слоем доступа (черновик → `app_send_draft`), доставляются конвейером под
`feelings_worker` (`pipeline_deliver` / `pipeline_withhold`), читаются по HTTP. Данные выдуманные;
тексты помечены метками — утечки ищутся поиском меток."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
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
from app.repositories import texts
from tests.contract import assert_matches
from tests.test_auth_api import SECRET, TOKEN, _init_data, _tg
from tests.test_couple_api import BOT, LINK_RE

SM = async_sessionmaker[AsyncSession]
FEED = "/api/v1/couple/messages"


@dataclass
class U:
    headers: dict[str, str]
    id: uuid.UUID
    agent_id: uuid.UUID | None = None


@dataclass
class Env:
    client: httpx.AsyncClient
    keys: Keyring
    engine: AsyncEngine
    sm: SM
    marks: list[str] = field(default_factory=list)

    def mark(self, what: str) -> str:
        label = f"метка-{what}-{uuid.uuid4().hex}"
        self.marks.append(label)
        return label


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
        yield Env(c, keys, engine, sessionmaker)
    app.dependency_overrides.clear()
    app.state.sessionmaker, app.state.keyring = prev


async def _login(env: Env) -> U:
    resp = await env.client.post("/api/v1/auth/telegram", json={"init_data": _init_data(_tg())})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return U({"Authorization": f"Bearer {body['access_token']}"}, uuid.UUID(body["me"]["id"]))


async def _person(env: Env, name: str, partner: str = "Партнёр") -> U:
    user = await _login(env)
    resp = await env.client.put(
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
    async with env.engine.connect() as conn:
        agent = (
            await conn.execute(text("SELECT id FROM agents WHERE user_id = :u"), {"u": user.id})
        ).scalar_one()
    user.agent_id = uuid.UUID(str(agent))
    return user


async def _pair(env: Env, a: U, b: U) -> None:
    resp = await env.client.post("/api/v1/couple", headers=a.headers)
    assert resp.status_code == 201, resp.text
    match = LINK_RE.match(resp.json()["link"])
    assert match
    resp = await env.client.post(f"/api/v1/invites/{match.group(1)}/accept", headers=b.headers)
    assert resp.status_code == 200, resp.text


async def send(env: Env, author: U, body: str, reply_to: uuid.UUID | None = None) -> uuid.UUID:
    assert author.agent_id is not None
    draft = await texts.create_draft(env.sm, env.keys, author.id, author.agent_id, body, reply_to)
    return await texts.send_draft(env.sm, author.id, draft, 1)


async def deliver(env: Env, msg: uuid.UUID, shown: str, *, adapted: bool) -> None:
    async with env.engine.connect() as conn, conn.begin():
        await conn.execute(text("SET LOCAL ROLE feelings_worker"))
        ok = (
            await conn.execute(
                text("SELECT pipeline_deliver(:m, :t, :ad, 2::smallint, :h)"),
                {
                    "m": msg,
                    "t": env.keys.seal(crypto.MESSAGE_ADAPTED, msg, shown),
                    "ad": adapted,
                    "h": adapted,
                },
            )
        ).scalar_one()
    assert ok is True


async def withhold(env: Env, msg: uuid.UUID) -> None:
    async with env.engine.connect() as conn, conn.begin():
        await conn.execute(text("SET LOCAL ROLE feelings_worker"))
        assert (
            await conn.execute(text("SELECT pipeline_withhold(:m)"), {"m": msg})
        ).scalar_one() is True


async def viewed_at(env: Env, msg: uuid.UUID) -> Any:
    async with env.engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT original_viewed_at FROM partner_messages WHERE id = :m"), {"m": msg}
            )
        ).scalar_one()


async def feed(env: Env, user: U, **params: Any) -> dict[str, Any]:
    resp = await env.client.get(FEED, headers=user.headers, params=params or None)
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    page: dict[str, Any] = resp.json()
    for item in page["items"]:
        assert_matches(item, "PartnerMessage")
    return page


def _error(resp: httpx.Response, status: int, code: str) -> None:
    assert resp.status_code == status, resp.text
    assert_matches(resp.json(), "Error")
    assert resp.json()["code"] == code, resp.json()


@dataclass
class Scene:
    anna: U
    igor: U
    olga: U
    texts: dict[str, str]
    ids: dict[str, uuid.UUID]


async def scene(env: Env) -> Scene:
    """Анна и Игорь в активной паре, посторонняя Ольга. От Анны: адаптированное, без адаптации,
    остановленное (withheld) и в конвейере (processing)."""
    anna, igor = await _person(env, "Анна", "Игорь"), await _person(env, "Игорь", "Анна")
    olga = await _person(env, "Ольга", "Пётр")
    await _pair(env, anna, igor)
    sc = Scene(anna, igor, olga, {}, {})
    for kind in ("adapted", "plain", "withheld", "processing"):
        sc.texts[kind] = env.mark(f"orig-{kind}")
        sc.ids[kind] = await send(env, anna, sc.texts[kind])
    sc.texts["soft"] = env.mark("soft")
    await deliver(env, sc.ids["adapted"], sc.texts["soft"], adapted=True)
    await deliver(env, sc.ids["plain"], sc.texts["plain"], adapted=False)
    await withhold(env, sc.ids["withheld"])
    return sc


# --------------------------------------------------------------------------- лента


async def test_feed_author_sees_originals_recipient_sees_delivered_versions(env: Env) -> None:
    """Автору — его оригиналы во всех статусах (версии для получателя нет, АР-25 п.3);
    получателю — только доставленные, версия для получателя с пометкой «адаптировано»."""
    sc = await scene(env)
    mine = {i["id"]: i for i in (await feed(env, sc.anna))["items"]}
    assert set(mine) == {str(v) for v in sc.ids.values()}
    for kind, msg in sc.ids.items():
        item = mine[str(msg)]
        assert item["direction"] == "outgoing" and item["text"] == sc.texts[kind]
    assert mine[str(sc.ids["withheld"])]["status"] == "withheld"
    assert mine[str(sc.ids["processing"])]["status"] == "processing"
    assert all(sc.texts["soft"] not in i["text"] for i in mine.values())  # версия Игоря — не автору
    got = (await feed(env, sc.igor))["items"]
    assert {i["id"] for i in got} == {str(sc.ids["adapted"]), str(sc.ids["plain"])}
    by_id = {i["id"]: i for i in got}
    adapted, plain = by_id[str(sc.ids["adapted"])], by_id[str(sc.ids["plain"])]
    assert (adapted["direction"], adapted["text"], adapted["is_adapted"]) == (
        "incoming",
        sc.texts["soft"],
        True,
    )
    assert adapted["harsh_tone_warning"] is True and adapted["status"] == "delivered"
    assert (plain["text"], plain["is_adapted"]) == (sc.texts["plain"], False)
    assert all(sc.texts["adapted"] not in i["text"] for i in got)  # оригинал — только по запросу
    assert [i["sent_at"] for i in got] == sorted((i["sent_at"] for i in got), reverse=True)


async def test_feed_isolation_outsider_and_ex_partner(env: Env) -> None:
    """Посторонняя не видит ничего; после выхода из пары лента пуста у обоих, оригинал и
    «прочитано» — 404 (обмен прекращается, ТЗ §4.5 п.2)."""
    sc = await scene(env)
    assert (await feed(env, sc.olga))["items"] == []
    for kind in ("adapted", "processing"):
        msg = sc.ids[kind]
        _error(
            await env.client.get(f"{FEED}/{msg}/original", headers=sc.olga.headers),
            404,
            "not_found",
        )
        _error(
            await env.client.post(f"{FEED}/{msg}/read", headers=sc.olga.headers), 404, "not_found"
        )
    assert (
        await env.client.post("/api/v1/couple/leave", headers=sc.igor.headers)
    ).status_code == 204
    for who in (sc.anna, sc.igor):
        assert (await feed(env, who))["items"] == []
    msg = sc.ids["adapted"]
    _error(
        await env.client.get(f"{FEED}/{msg}/original", headers=sc.igor.headers), 404, "not_found"
    )
    _error(await env.client.post(f"{FEED}/{msg}/read", headers=sc.igor.headers), 404, "not_found")
    # Новая пара Игоря не видит сообщений бывшей.
    olga2 = await _person(env, "Ольга-2", "Игорь")
    await _pair(env, sc.igor, olga2)
    assert (await feed(env, sc.igor))["items"] == []
    assert await viewed_at(env, msg) is None


async def test_feed_cursor_newest_first_and_foreign_cursor_422(env: Env) -> None:
    anna, igor = await _person(env, "Анна", "Игорь"), await _person(env, "Игорь", "Анна")
    await _pair(env, anna, igor)
    sent = []
    for i in range(5):
        msg = await send(env, anna, f"сообщение {i}")
        await deliver(env, msg, f"сообщение {i}", adapted=False)
        sent.append(str(msg))
    page = await feed(env, igor, limit=2)
    pages = [page["items"]]
    late = await send(env, anna, "позднее")  # после выдачи курсора — не на следующих страницах
    await deliver(env, late, "позднее", adapted=False)
    while page["next_cursor"] is not None:
        page = await feed(env, igor, limit=2, cursor=page["next_cursor"])
        pages.append(page["items"])
    assert [len(p) for p in pages] == [2, 2, 1]
    assert [i["id"] for p in pages for i in p] == sent[::-1]
    assert (await feed(env, igor, limit=1))["items"][0]["id"] == str(late)
    cursor = (await feed(env, igor, limit=1))["next_cursor"]
    assert cursor
    for bad in (cursor + "x", "abc", "a.b.c"):
        _error(
            await env.client.get(FEED, headers=igor.headers, params={"cursor": bad}),
            422,
            "validation_error",
        )
    # Курсор Игоря не годится Анне (подпись привязана к владельцу).
    _error(
        await env.client.get(FEED, headers=anna.headers, params={"cursor": cursor}),
        422,
        "validation_error",
    )
    for bad_limit in ("0", "101"):
        _error(
            await env.client.get(FEED, headers=igor.headers, params={"limit": bad_limit}),
            422,
            "validation_error",
        )


async def test_feed_shows_undecryptable_as_unavailable_text(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    """Контракт (АР-40): `PartnerMessage.text` — `ExportText`. Нерасшифрованное не скрывается —
    `UnavailableText` с кодом причины, лента не падает; в журнале — код причины без id."""
    caplog.set_level(logging.DEBUG)
    anna, igor = await _person(env, "Анна", "Игорь"), await _person(env, "Игорь", "Анна")
    await _pair(env, anna, igor)
    ok = await send(env, anna, "читается")
    await deliver(env, ok, "читается", adapted=False)
    bad = await send(env, anna, "испортится")
    async with env.engine.connect() as conn, conn.begin():
        await conn.execute(text("SET LOCAL ROLE feelings_worker"))
        await conn.execute(
            text("SELECT pipeline_deliver(:m, :t, false, 0::smallint, false)"),
            {"m": bad, "t": b"\x00broken"},
        )
    items = {i["id"]: i for i in (await feed(env, igor))["items"]}
    assert set(items) == {str(ok), str(bad)}
    assert items[str(ok)]["text"] == "читается"
    assert items[str(bad)]["text"]["unavailable"] is True
    assert_matches(items[str(bad)]["text"], "UnavailableText")
    assert "partner_messages.adapted_enc" in caplog.text and str(bad) not in caplog.text


# --------------------------------------------------------------------------- оригинал


async def test_original_to_recipient_records_view_once(env: Env) -> None:
    """Получателю доставленного — оригинал, просмотр фиксируется (повтор не сдвигает отметку);
    автору — свой текст без фиксации; недоставленное получателю — 404."""
    sc = await scene(env)
    msg = sc.ids["adapted"]
    assert await viewed_at(env, msg) is None
    resp = await env.client.get(f"{FEED}/{msg}/original", headers=sc.anna.headers)
    assert resp.status_code == 200 and resp.json() == {"text": sc.texts["adapted"]}
    assert await viewed_at(env, msg) is None  # автор — без фиксации
    resp = await env.client.get(f"{FEED}/{msg}/original", headers=sc.igor.headers)
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    assert resp.json() == {"text": sc.texts["adapted"]}
    first = await viewed_at(env, msg)
    assert first is not None
    again = await env.client.get(f"{FEED}/{msg}/original", headers=sc.igor.headers)
    assert again.status_code == 200 and await viewed_at(env, msg) == first
    for kind in ("withheld", "processing"):
        _error(
            await env.client.get(f"{FEED}/{sc.ids[kind]}/original", headers=sc.igor.headers),
            404,
            "not_found",
        )
        assert await viewed_at(env, sc.ids[kind]) is None
    for target in (uuid.uuid4(), "not-a-uuid"):
        _error(
            await env.client.get(f"{FEED}/{target}/original", headers=sc.igor.headers),
            404,
            "not_found",
        )


async def test_original_undecryptable_is_unavailable_and_view_not_recorded(env: Env) -> None:
    """АР-40: оригинал не расшифровался — 200 с `UnavailableText`, просмотр не фиксируется."""
    sc = await scene(env)
    msg = sc.ids["plain"]
    async with env.engine.begin() as conn:  # черновик потерян — контекст оригинала недоступен
        await conn.execute(
            text("UPDATE partner_messages SET original_enc = :b WHERE id = :m"),
            {"b": b"\x00broken", "m": msg},
        )
    resp = await env.client.get(f"{FEED}/{msg}/original", headers=sc.igor.headers)
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    assert resp.json()["text"]["unavailable"] is True
    assert_matches(resp.json()["text"], "UnavailableText")
    assert await viewed_at(env, msg) is None


async def test_original_conflict_is_409(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """40P01 в транзакции чтения оригинала — 409 `conflict_retry` (АР-24); отметки нет."""
    from app.repositories import couple_messages

    sc = await scene(env)
    real = couple_messages._visible

    async def conflict(s: AsyncSession, *a: Any) -> Any:
        row = await real(s, *a)
        await s.execute(text("DO $$ BEGIN RAISE EXCEPTION 'x' USING ERRCODE = '40P01'; END $$"))
        return row

    monkeypatch.setattr(couple_messages, "_visible", conflict)
    msg = sc.ids["adapted"]
    _error(
        await env.client.get(f"{FEED}/{msg}/original", headers=sc.igor.headers),
        409,
        "conflict_retry",
    )
    assert await viewed_at(env, msg) is None


# --------------------------------------------------------------------------- статусы


async def test_read_then_answered_statuses(env: Env) -> None:
    """«Прочитано» — получатель доставленного (повтор — 204 без изменений); автор, чужой и
    недоставленное — 404; ответ (`reply_to_id`) после доставки — «отвечено»."""
    sc = await scene(env)
    msg = sc.ids["adapted"]
    _error(await env.client.post(f"{FEED}/{msg}/read", headers=sc.anna.headers), 404, "not_found")
    _error(
        await env.client.post(f"{FEED}/{sc.ids['withheld']}/read", headers=sc.igor.headers),
        404,
        "not_found",
    )
    resp = await env.client.post(f"{FEED}/{msg}/read", headers=sc.igor.headers)
    assert resp.status_code == 204 and resp.content == b""
    item = next(i for i in (await feed(env, sc.anna))["items"] if i["id"] == str(msg))
    assert item["status"] == "read" and item["read_at"] is not None
    assert (await env.client.post(f"{FEED}/{msg}/read", headers=sc.igor.headers)).status_code == 204
    item2 = next(i for i in (await feed(env, sc.anna))["items"] if i["id"] == str(msg))
    assert item2["read_at"] == item["read_at"]
    reply = await send(env, sc.igor, "ответ", reply_to=msg)
    await deliver(env, reply, "ответ", adapted=False)
    items = {i["id"]: i for i in (await feed(env, sc.anna))["items"]}
    assert items[str(msg)]["status"] == "answered"
    assert items[str(reply)]["reply_to_id"] == str(msg)
    assert (await env.client.post(f"{FEED}/{msg}/read", headers=sc.igor.headers)).status_code == 204
    items = {i["id"]: i for i in (await feed(env, sc.anna))["items"]}
    assert items[str(msg)]["status"] == "answered"  # «прочитано» не откатывает «отвечено»


# --------------------------------------------------------------------------- доступ и журналы


async def test_access_401_403(env: Env) -> None:
    fresh = await _login(env)
    for path, method in ((FEED, "GET"), (f"{FEED}/{uuid.uuid4()}/original", "GET"),
                         (f"{FEED}/{uuid.uuid4()}/read", "POST")):  # fmt: skip
        for headers in ({}, {"Authorization": "Bearer nope"}):
            _error(await env.client.request(method, path, headers=headers), 401, "unauthorized")
        _error(
            await env.client.request(method, path, headers=fresh.headers),
            403,
            "onboarding_required",
        )


async def test_logs_have_no_texts_or_ids(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    sc = await scene(env)
    await feed(env, sc.anna)
    await feed(env, sc.igor)
    await env.client.get(f"{FEED}/{sc.ids['adapted']}/original", headers=sc.igor.headers)
    await env.client.post(f"{FEED}/{sc.ids['adapted']}/read", headers=sc.igor.headers)
    logged = "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)
    for label in env.marks:
        assert label not in logged and label.split("-")[-1] not in logged
    assert str(sc.anna.id) not in logged and str(sc.igor.id) not in logged


# --------------------------------------------------------------------------- причина остановки
# 3.2 (A2), Б-84 / A8-3.2-D2: автору у `withheld` — `withheld_reason` и `withheld_crisis`
# (контракт `PartnerMessage`, АР-44, АР-45); получателю остановленное не видно и полей нет.

CRISES = ("violence", "threat_outgoing", "self_harm", "life_threat")
NO_WITHHELD = (None, None)


async def withhold_leased(env: Env, msg: uuid.UUID, reason: str, crisis: str | None = None) -> None:
    """Остановка, как у воркера: аренда (выдана «захватом» — здесь проставлена напрямую, чтобы
    не забирать чужие сообщения общей тестовой БД) → ``pipeline_withhold_leased`` (0014)."""
    token = uuid.uuid4()
    async with env.engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "UPDATE partner_messages SET work_lease_token = :t, "
                "work_lease_until = now() + interval '1 minute' WHERE id = :m"
            ),
            {"m": msg, "t": token},
        )
        await conn.execute(text("SET LOCAL ROLE feelings_worker"))
        ok = (
            await conn.execute(
                text(
                    "SELECT pipeline_withhold_leased(:m, :t, CAST(:r AS varchar), "
                    "CAST(:ev AS safety_event_type[]), CAST(:c AS safety_event_type))"
                ),
                {"m": msg, "t": token, "r": reason, "ev": [crisis] if crisis else [], "c": crisis},
            )
        ).scalar_one()
    assert ok is True


async def exhaust(env: Env, msg: uuid.UUID) -> None:
    """``attempts_exhausted`` ставит сам ``pipeline_claim`` (без модели); общий захват забрал
    бы сообщения других тестов — исход проставлен напрямую, как его пишет функция."""
    async with env.engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "UPDATE partner_messages SET status = 'withheld', "
                "withheld_reason = 'attempts_exhausted' WHERE id = :m AND status = 'processing'"
            ),
            {"m": msg},
        )


def _withheld(item: dict[str, Any]) -> tuple[Any, Any]:
    return item.get("withheld_reason"), item.get("withheld_crisis")


async def reasons_scene(env: Env) -> tuple[Scene, dict[str, tuple[str | None, str | None]]]:
    """К сцене (доставленные, ``processing``, остановленное прежней функцией — ``safety`` без
    типа) — остановки Анны по каждой причине и каждому типу кризиса и остановка Игоря."""
    sc = await scene(env)
    want: dict[str, tuple[str | None, str | None]] = {
        "adapted": NO_WITHHELD,
        "plain": NO_WITHHELD,
        "processing": NO_WITHHELD,
        "withheld": ("safety", None),  # до 0014: тип неизвестен — общий протокол
    }
    cases = [("safety", c) for c in CRISES] + [
        ("model_refusal", None),
        ("pipeline_error", None),
    ]
    for reason, crisis in cases:
        kind = f"{reason}-{crisis}"
        sc.texts[kind] = env.mark(f"orig-{kind}")
        sc.ids[kind] = await send(env, sc.anna, sc.texts[kind])
        await withhold_leased(env, sc.ids[kind], reason, crisis)
        want[kind] = (reason, crisis)
    sc.texts["exhausted"] = env.mark("orig-exhausted")
    sc.ids["exhausted"] = await send(env, sc.anna, sc.texts["exhausted"])
    await exhaust(env, sc.ids["exhausted"])
    want["exhausted"] = ("attempts_exhausted", None)
    sc.texts["igor"] = env.mark("orig-igor")
    sc.ids["igor"] = await send(env, sc.igor, sc.texts["igor"])
    await withhold_leased(env, sc.ids["igor"], "safety", "threat_outgoing")
    return sc, want


async def test_feed_author_sees_withheld_reason_and_crisis(env: Env) -> None:
    """§9.3, АР-28, АР-44, АР-45: автору у остановленного — причина и тип кризиса (какой
    протокол помощи показать); у доставленных и ``processing`` — null. Ответ — по схеме
    контракта (``feed`` сверяет каждый элемент)."""
    sc, want = await reasons_scene(env)
    mine = {i["id"]: i for i in (await feed(env, sc.anna))["items"]}
    anna_ids = {k: v for k, v in sc.ids.items() if k != "igor"}
    assert set(mine) == {str(v) for v in anna_ids.values()}
    for kind, msg in anna_ids.items():
        item = mine[str(msg)]
        assert _withheld(item) == want[kind], kind
        assert item["status"] == ("withheld" if want[kind][0] else item["status"])
        assert item["text"] == sc.texts[kind]  # текст сохранён у автора
    # Остановленное Игоря: Игорю — своё, с типом; Анне (получателю) — не видно.
    igor_item = {i["id"]: i for i in (await feed(env, sc.igor))["items"]}[str(sc.ids["igor"])]
    assert _withheld(igor_item) == ("safety", "threat_outgoing")


async def test_feed_recipient_sees_no_withheld_nor_reason_fields(env: Env) -> None:
    """Получателю остановленное и ``processing`` не видны; у видимых (входящих) причины и типа
    нет — ни у одного элемента ленты получателя (АР-11, АР-25, АР-44)."""
    sc, want = await reasons_scene(env)
    got = (await feed(env, sc.igor))["items"]
    delivered = {str(sc.ids["adapted"]), str(sc.ids["plain"])}
    incoming = [i for i in got if i["direction"] == "incoming"]
    assert {i["id"] for i in incoming} == delivered
    for item in incoming:
        assert _withheld(item) == NO_WITHHELD
        assert item["status"] in ("delivered", "read", "answered")
    anna_feed = (await feed(env, sc.anna))["items"]
    assert str(sc.ids["igor"]) not in {i["id"] for i in anna_feed}
    assert all(_withheld(i) == NO_WITHHELD for i in anna_feed if i["direction"] == "incoming")
    hidden = [t for k, t in sc.texts.items() if want.get(k, NO_WITHHELD)[0] and k != "igor"]
    assert all(t not in i["text"] for i in got for t in hidden)


def test_repository_drops_withheld_fields_outside_own_withheld() -> None:
    """Вторая граница слоя доступа: даже если строка (подделка, ошибка функции) несёт причину у
    входящего или не остановленного сообщения, в ``FeedItem`` её нет; тип кризиса — только при
    ``safety``."""
    from types import SimpleNamespace

    from app.repositories.couple_messages import _withheld as pick

    def row(outgoing: bool, status: str, reason: str | None, crisis: str | None) -> Any:
        return SimpleNamespace(
            outgoing=outgoing, status=status, withheld_reason=reason, withheld_crisis=crisis
        )

    none = {"withheld_reason": None, "withheld_crisis": None}
    assert pick(row(False, "withheld", "safety", "violence")) == none
    assert pick(row(False, "delivered", "safety", "violence")) == none
    assert pick(row(True, "processing", "model_refusal", None)) == none
    assert pick(row(True, "withheld", "model_refusal", "violence")) == {
        "withheld_reason": "model_refusal",
        "withheld_crisis": None,
    }
    assert pick(row(True, "withheld", "safety", "self_harm")) == {
        "withheld_reason": "safety",
        "withheld_crisis": "self_harm",
    }


async def test_send_202_has_null_withheld_fields(env: Env) -> None:
    """202 ``POST /drafts/{id}/send``: сообщение в ``processing`` — причины ещё нет (null), ответ
    по схеме контракта ``PartnerMessage``."""
    anna, igor = await _person(env, "Анна", "Игорь"), await _person(env, "Игорь", "Анна")
    await _pair(env, anna, igor)
    assert anna.agent_id is not None
    body = env.mark("send-202")
    draft = await texts.create_draft(env.sm, env.keys, anna.id, anna.agent_id, body, None)
    resp = await env.client.post(
        f"/api/v1/drafts/{draft}/send", headers=anna.headers, json={"expected_version": 1}
    )
    assert resp.status_code == 202, resp.text
    out = resp.json()
    assert_matches(out, "PartnerMessage")
    assert out["status"] == "processing" and out["text"] == body
    assert _withheld(out) == NO_WITHHELD
    assert out["withheld_reason"] is None and out["withheld_crisis"] is None


async def test_withheld_feed_logs_have_no_texts(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    sc, _ = await reasons_scene(env)
    await feed(env, sc.anna)
    await feed(env, sc.igor)
    logged = "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)
    for label in env.marks:
        assert label not in logged and label.split("-")[-1] not in logged
    assert str(sc.anna.id) not in logged and str(sc.igor.id) not in logged

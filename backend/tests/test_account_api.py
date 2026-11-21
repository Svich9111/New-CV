"""4.5, этап 2 (A2): `DELETE /me` и `GET /me/export` по HTTP; уведомления после удаления.

Критерии — docs/tasks/4.5.md («Критерии готовности (этап 2)»); ФТ-21; ТЗ §2 п.3, §4.5 п.3, §7,
§9.1 п.5–6, §9.3; DECISIONS.md АР-17, АР-23, АР-24, АР-25. Тексты заводятся через слой доступа
(шифрование 4.2), конвейер — под `feelings_worker`, подписки — суперпользователем. Все данные
выдуманные; каждый текст помечен уникальной меткой — изоляция проверяется поиском меток в
сериализованном JSON выгрузки."""

import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.bot.client import BotApi
from app.bot.notifications import NotificationKind, notify_user
from app.config import get_settings
from app.core.crypto import Keyring, generate_key_entry, parse_keyring
from app.core.session import issue_token
from app.db import user_scope
from app.main import app
from app.repositories import account, texts
from tests.contract import assert_matches
from tests.test_account_export_db import _deliver, _withhold
from tests.test_auth_api import SECRET, _init_data, _tg
from tests.test_couple_api import _create, _error, _settings

SM = async_sessionmaker[AsyncSession]
WEBAPP = "https://app.example.test/"


@pytest.fixture
def keys() -> Keyring:
    return parse_keyring(generate_key_entry("k1"), "k1")


@pytest.fixture
async def client(sessionmaker: SM, keys: Keyring) -> AsyncIterator[httpx.AsyncClient]:
    app.state.sessionmaker = sessionmaker
    prev_keys, app.state.keyring = app.state.keyring, keys
    app.dependency_overrides[get_settings] = _settings
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()
    app.state.keyring = prev_keys


@dataclass
class U:
    headers: dict[str, str]
    id: uuid.UUID
    tg: int
    agent_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    marks: dict[str, str] = field(default_factory=dict)  # что → уникальная метка в тексте


async def _login(client: httpx.AsyncClient, tg: int | None = None) -> U:
    tg = tg or _tg()
    resp = await client.post("/api/v1/auth/telegram", json={"init_data": _init_data(tg)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return U({"Authorization": f"Bearer {body['access_token']}"}, uuid.UUID(body["me"]["id"]), tg)


async def _onboard(client: httpx.AsyncClient, user: U, name: str, partner_name: str) -> None:
    resp = await client.put(
        "/api/v1/me/onboarding",
        headers=user.headers,
        json={
            "name": name,
            "address_form": "на ты",
            "partner_name": partner_name,
            "timezone": "Europe/Moscow",
            "consent_terms": True,
            "consent_data": True,
            "consent_sensitive": True,
            "age_confirmed": True,
        },
    )
    assert resp.status_code == 200, resp.text


def _mark(user: U, what: str) -> str:
    label = f"метка-{what}-{uuid.uuid4().hex}"
    user.marks[what] = label
    return label


async def _fill(sm: SM, engine: AsyncEngine, keys: Keyring, user: U) -> None:
    """Агент (создан онбордингом), диалог с репликами и итогом, профиль агента, память,
    черновик и настройки тона — каждый текст со своей меткой."""
    async with engine.begin() as conn:
        agent = (
            await conn.execute(text("SELECT id FROM agents WHERE user_id = :u"), {"u": user.id})
        ).scalar_one()
        user.agent_id = uuid.UUID(str(agent))
        user.conversation_id = uuid.uuid4()
        await conn.execute(
            text("INSERT INTO conversations (id, agent_id) VALUES (:c, :a)"),
            {"c": user.conversation_id, "a": user.agent_id},
        )
        await conn.execute(
            text(
                "UPDATE agents SET tone_settings = jsonb_build_object('note', CAST(:m AS text)) "
                "WHERE id = :a"
            ),
            {"a": user.agent_id, "m": _mark(user, "tone")},
        )
    for author, what in (("owner", "chat-owner"), ("agent", "chat-agent")):
        await texts.add_conversation_message(
            sm, keys, user.id, user.conversation_id, author, _mark(user, what)
        )
    await texts.set_conversation_summary(
        sm, keys, user.id, user.conversation_id, _mark(user, "summary")
    )
    await texts.set_agent_profile_summary(sm, keys, user.id, user.agent_id, _mark(user, "profile"))
    await texts.add_memory_item(
        sm, keys, user.id, user.agent_id, "fact", "owner_dialog", _mark(user, "memory")
    )
    await texts.create_draft(sm, keys, user.id, user.agent_id, _mark(user, "draft"))


async def _send(sm: SM, keys: Keyring, user: U, what: str) -> tuple[uuid.UUID, uuid.UUID]:
    assert user.agent_id is not None
    draft = await texts.create_draft(sm, keys, user.id, user.agent_id, _mark(user, what))
    return draft, await texts.send_draft(sm, user.id, draft, 1)


@dataclass
class Scene:
    anna: U
    igor: U
    olga: U
    couple: str
    msgs: dict[str, tuple[uuid.UUID, uuid.UUID]]  # метка сообщения → (draft_id, message_id)


async def _scene(client: httpx.AsyncClient, sm: SM, engine: AsyncEngine, keys: Keyring) -> Scene:
    """Анна и Игорь — активная пара, Ольга — посторонняя. У Анны и Игоря по сообщению:
    доставленное адаптированным, доставленное без адаптации, остановленное, в конвейере."""
    anna, igor, olga = await _login(client), await _login(client), await _login(client)
    await _onboard(client, anna, "Анна", "Игорёк")
    await _onboard(client, igor, "Игорь", "Анечка")
    await _onboard(client, olga, "Ольга", "Пётр")
    for u in (anna, igor, olga):
        await _fill(sm, engine, keys, u)
    token = await _create(client, anna)
    resp = await client.post(f"/api/v1/invites/{token}/accept", headers=igor.headers)
    assert resp.status_code == 200, resp.text
    couple = resp.json()["id"]
    msgs: dict[str, tuple[uuid.UUID, uuid.UUID]] = {}
    for who, u in (("a", anna), ("i", igor)):
        for kind in ("adapted", "plain", "withheld", "processing"):
            label = f"{who}-{kind}"
            msgs[label] = await _send(sm, keys, u, f"msg-{kind}")
            mid = msgs[label][1]
            if kind == "adapted":
                await _deliver(engine, keys, mid, _mark(u, "msg-for-recipient"), adapted=True)
            elif kind == "plain":
                await _deliver(engine, keys, mid, u.marks["msg-plain"], adapted=False)
            elif kind == "withheld":
                await _withhold(engine, mid)
    return Scene(anna, igor, olga, couple, msgs)


async def _export(client: httpx.AsyncClient, user: U) -> tuple[dict[str, Any], str]:
    resp = await client.get("/api/v1/me/export", headers=user.headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert_matches(body, "AccountExport")
    return body, resp.text


def _no_marks(raw: str, user: U, *, only: tuple[str, ...] | None = None) -> None:
    for what, label in user.marks.items():
        if only is None or what in only:
            assert label not in raw, what


async def _viewed_at(engine: AsyncEngine, msg: uuid.UUID) -> datetime | None:
    async with engine.connect() as conn:
        value = (
            await conn.execute(
                text("SELECT original_viewed_at FROM partner_messages WHERE id = :m"), {"m": msg}
            )
        ).scalar_one()
    return value if value is None else value.astimezone(UTC)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


async def _raise_sqlstate(sm: SM, user_id: uuid.UUID, code: str) -> None:
    """Настоящая ошибка БД с нужным SQLSTATE тем же путём, что у функций (asyncpg → DBAPIError)."""
    async with user_scope(sm, user_id) as s:
        await s.execute(
            text(f"DO $$ BEGIN RAISE EXCEPTION 'conflict' USING ERRCODE = '{code}'; END $$")
        )


# --- DELETE /me -------------------------------------------------------------------------------


async def test_delete_me_then_token_is_dead(
    client: httpx.AsyncClient, sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """204 без тела; затем любой запрос с тем же токеном — 401, повтор удаления — 401; пара
    распущена для партнёра; повторный вход тем же Telegram ID — новый пользователь."""
    sc = await _scene(client, sessionmaker, engine, keys)
    resp = await client.delete("/api/v1/me", headers=sc.anna.headers)
    assert resp.status_code == 204 and resp.content == b""
    for method, path in (
        ("GET", "/api/v1/me"),
        ("GET", "/api/v1/me/export"),
        ("DELETE", "/api/v1/me"),
        ("POST", "/api/v1/couple/leave"),
        ("GET", "/api/v1/couple"),
    ):
        _error(await client.request(method, path, headers=sc.anna.headers), 401, "unauthorized")
    async with engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM users WHERE id = :u"), {"u": sc.anna.id})
        ).scalar_one() == 0
    _error(await client.get("/api/v1/couple", headers=sc.igor.headers), 404)
    again = await _login(client, sc.anna.tg)
    assert again.id != sc.anna.id
    me = (await client.get("/api/v1/me", headers=again.headers)).json()
    assert me["onboarding_completed"] is False and me["partner_name"] is None


async def test_delete_me_without_onboarding(client: httpx.AsyncClient) -> None:
    """АР-17, АР-25: удаление доступно сразу после входа, без онбординга (не 403)."""
    user = await _login(client)
    assert (await client.delete("/api/v1/me", headers=user.headers)).status_code == 204
    _error(await client.get("/api/v1/me", headers=user.headers), 401, "unauthorized")


async def test_delete_me_after_underage_refusal(client: httpx.AsyncClient) -> None:
    """Снявший 18+ (доступ закрыт, ТЗ §9.3) всё равно может удалить свои данные."""
    user = await _login(client)
    body = {
        "name": "Тест",
        "address_form": "на ты",
        "partner_name": "Тестовый",
        "consent_terms": True,
        "consent_data": True,
        "consent_sensitive": True,
        "age_confirmed": False,
    }
    _error(
        await client.put("/api/v1/me/onboarding", headers=user.headers, json=body), 403, "underage"
    )
    assert (await client.delete("/api/v1/me", headers=user.headers)).status_code == 204


async def test_delete_me_requires_session(client: httpx.AsyncClient) -> None:
    """Без токена, с токеном чужой подписи и с подлинным токеном несуществующего пользователя —
    401; ничего не удаляется."""
    victim = await _login(client)
    forged = issue_token(victim.id, "x" * 40, 600)  # подпись не тем секретом
    ghost = issue_token(uuid.uuid4(), SECRET, 600)  # подлинная подпись, пользователя нет
    for headers in (
        {},
        {"Authorization": f"Bearer {forged}"},
        {"Authorization": f"Bearer {ghost}"},
    ):
        _error(await client.delete("/api/v1/me", headers=headers), 401, "unauthorized")
        _error(await client.get("/api/v1/me/export", headers=headers), 401, "unauthorized")
    assert (await client.get("/api/v1/me", headers=victim.headers)).status_code == 200


async def test_delete_me_conflict_is_409_and_changes_nothing(
    client: httpx.AsyncClient, sessionmaker: SM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """АР-24: 40P01/40001 → 409 conflict_retry без деталей БД; транзакция отменена — аккаунт
    на месте, повтор проходит."""
    user = await _login(client)
    for code in ("40P01", "40001"):

        async def _conflict(sm: SM, user_id: uuid.UUID, code: str = code) -> None:
            await _raise_sqlstate(sm, user_id, code)

        monkeypatch.setattr(account, "delete_account", _conflict)
        resp = await client.delete("/api/v1/me", headers=user.headers)
        _error(resp, 409, "conflict_retry")
        assert "conflict" not in resp.json()["message"] and code not in resp.text
        assert (await client.get("/api/v1/me", headers=user.headers)).status_code == 200
    monkeypatch.undo()
    assert (await client.delete("/api/v1/me", headers=user.headers)).status_code == 204


async def test_other_db_errors_are_not_409(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Общий обработчик ловит только 40P01/40001: прочие ошибки БД не маскируются под повтор."""
    user = await _login(client)

    async def _broken(sm: SM, user_id: uuid.UUID) -> None:
        await _raise_sqlstate(sm, user_id, "XX000")

    monkeypatch.setattr(account, "delete_account", _broken)
    with pytest.raises(DBAPIError):
        await client.delete("/api/v1/me", headers=user.headers)


async def test_export_conflict_is_409(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _login(client)

    async def _conflict(sm: SM, keys: Keyring, user_id: uuid.UUID) -> None:
        await _raise_sqlstate(sm, user_id, "40P01")

    monkeypatch.setattr(account, "export_account", _conflict)
    _error(await client.get("/api/v1/me/export", headers=user.headers), 409, "conflict_retry")


async def test_delete_and_export_of_vanished_user_is_401(sessionmaker: SM, keys: Keyring) -> None:
    """Пользователь исчез между проверкой токена и вызовом (42501 функций) — AccountGone
    (обработчик отдаёт 401), а не 500."""
    ghost = uuid.uuid4()
    with pytest.raises(account.AccountGone):
        await account.delete_account(sessionmaker, ghost)
    with pytest.raises(account.AccountGone):
        await account.export_account(sessionmaker, keys, ghost)


# --- GET /me/export ---------------------------------------------------------------------------


async def test_export_full_composition(
    client: httpx.AsyncClient, sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """Все разделы по схеме AccountExport, тексты расшифрованы; о партнёре — только имя,
    которым его назвал сам пользователь; версия для получателя своих сообщений и id черновиков
    партнёра не выгружаются."""
    sc = await _scene(client, sessionmaker, engine, keys)
    anna, igor, m = sc.anna, sc.igor, sc.msgs
    async with user_scope(sessionmaker, anna.id) as s:
        assert (
            await s.execute(text("SELECT app_mark_read(:m)"), {"m": m["i-plain"][1]})
        ).scalar_one() is True
    # Подписка, где Анна — плательщик, с платежом; и подписка Игоря (её у Анны быть не должно).
    sub_a, sub_i, pay_a = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    charge_i = f"charge-{uuid.uuid4().hex}"
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        for sub, payer in ((sub_a, anna.id), (sub_i, igor.id)):
            await conn.execute(
                text(
                    "INSERT INTO subscriptions (id, couple_id, payer_id, plan, status, "
                    "period_start, period_end) VALUES (:s, :c, :p, 'month', 'active', :a, :b)"
                ),
                {"s": sub, "c": sc.couple, "p": payer, "a": now, "b": now + timedelta(days=30)},
            )
        for pay, sub, charge in ((pay_a, sub_a, "charge-anna"), (uuid.uuid4(), sub_i, charge_i)):
            await conn.execute(
                text(
                    "INSERT INTO payments (id, subscription_id, amount, provider, "
                    "provider_charge_id, status) VALUES (:i, :s, 150, 'telegram_stars', :c, "
                    "'succeeded')"
                ),
                {"i": pay, "s": sub, "c": charge},
            )

    body, raw = await _export(client, anna)
    assert body["export_version"] == 1
    p = body["profile"]
    assert p["id"] == str(anna.id) and p["telegram_id"] == anna.tg
    assert (p["name"], p["partner_name"], p["onboarding_completed"]) == ("Анна", "Игорёк", True)
    assert body["agent"]["profile_summary"] == anna.marks["profile"]
    assert body["agent"]["tone_settings"] == {"note": anna.marks["tone"]}
    [conv] = body["conversations"]
    assert conv["id"] == str(anna.conversation_id) and conv["summary"] == anna.marks["summary"]
    assert [(x["author"], x["text"]) for x in conv["messages"]] == [
        ("owner", anna.marks["chat-owner"]),
        ("agent", anna.marks["chat-agent"]),
    ]
    [mem] = body["memory"]
    assert (mem["text"], mem["type"], mem["source"]) == (
        anna.marks["memory"],
        "fact",
        "owner_dialog",
    )
    drafts = {d["text"]: d for d in body["drafts"]}
    assert drafts[anna.marks["draft"]]["status"] == "draft"
    for kind in ("adapted", "plain", "withheld", "processing"):
        assert drafts[anna.marks[f"msg-{kind}"]]["status"] == "sent"

    sent = {x["id"]: x for x in body["couple_messages"]["sent"]}
    assert set(sent) == {
        str(m[f"a-{k}"][1]) for k in ("adapted", "plain", "withheld", "processing")
    }
    for kind, status in (
        ("adapted", "delivered"),
        ("plain", "delivered"),
        ("withheld", "withheld"),
        ("processing", "processing"),
    ):
        x = sent[str(m[f"a-{kind}"][1])]
        assert x["original"] == anna.marks[f"msg-{kind}"] and x["status"] == status
        assert x["couple_id"] == sc.couple
    assert sent[str(m["a-adapted"][1])]["is_adapted"] is True
    # Версия, которую агент Игоря сделал для Игоря, у автора не выгружается (АР-25 п.3).
    assert anna.marks["msg-for-recipient"] not in raw

    received = {x["id"]: x for x in body["couple_messages"]["received"]}
    assert set(received) == {str(m["i-adapted"][1]), str(m["i-plain"][1])}
    ad, pl = received[str(m["i-adapted"][1])], received[str(m["i-plain"][1])]
    assert (ad["text"], ad["original"], ad["is_adapted"]) == (
        igor.marks["msg-for-recipient"],
        igor.marks["msg-adapted"],
        True,
    )
    assert (pl["text"], pl["original"], pl["status"]) == (
        igor.marks["msg-plain"],
        igor.marks["msg-plain"],
        "read",
    )
    assert pl["read_at"] is not None
    # Остановленное и ещё не доставленное сообщение Игоря Анна не видела — их нет (ТЗ §9.3).
    _no_marks(raw, igor, only=("msg-withheld", "msg-processing"))
    # id черновиков Игоря (нужны только для расшифровки) в выгрузку не попадают.
    for label in ("i-adapted", "i-plain"):
        assert str(m[label][0]) not in raw

    [couple] = body["couples"]
    assert (couple["id"], couple["status"], couple["role"]) == (sc.couple, "active", "initiator")
    [sub] = body["subscriptions"]
    assert sub["id"] == str(sub_a) and sub["couple_id"] == sc.couple
    assert [(x["id"], x["provider_charge_id"]) for x in sub["payments"]] == [
        (str(pay_a), "charge-anna")
    ]
    assert str(sub_i) not in raw and charge_i not in raw
    assert body["safety_events"] == []
    # О партнёре — ни id, ни Telegram ID, ни имени из его профиля.
    assert str(igor.id) not in raw and str(igor.tg) not in raw and "Игорь" not in raw
    # Служебное: ни шифротекста, ни хешей, ни оценки тона.
    assert "tone_score" not in raw and "draft_id" not in raw and "fingerprint" not in raw


async def test_export_isolation_partner_and_stranger(
    client: httpx.AsyncClient, sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """Ни одного текста Анны в выгрузках Игоря и Ольги, кроме доставленных Игорю сообщений;
    память, личный чат, профиль агента, настройки тона и черновики партнёра не попадают."""
    sc = await _scene(client, sessionmaker, engine, keys)
    anna, igor, olga = sc.anna, sc.igor, sc.olga
    _, igor_raw = await _export(client, igor)
    _, olga_raw = await _export(client, olga)
    _, anna_raw = await _export(client, anna)
    private = ("tone", "chat-owner", "chat-agent", "summary", "profile", "memory", "draft")
    for raw in (igor_raw, olga_raw):
        _no_marks(raw, anna, only=(*private, "msg-withheld", "msg-processing"))
    _no_marks(anna_raw, igor, only=(*private, "msg-withheld", "msg-processing"))
    # Доставленные сообщения — у получателя есть (иначе проверка выше ничего бы не стоила).
    assert anna.marks["msg-plain"] in igor_raw and igor.marks["msg-plain"] in anna_raw
    # Посторонняя не видит ничего ни от кого из пары, и пара — ничего от неё.
    _no_marks(olga_raw, anna)
    _no_marks(olga_raw, igor)
    _no_marks(anna_raw, olga)
    _no_marks(igor_raw, olga)
    olga_body = json.loads(olga_raw)
    assert olga_body["couple_messages"] == {"sent": [], "received": []}
    assert olga_body["couples"] == [] and olga_body["subscriptions"] == []
    assert str(anna.id) not in olga_raw and str(anna.id) not in igor_raw


async def test_export_without_onboarding(client: httpx.AsyncClient) -> None:
    """АР-25: выгрузка доступна до онбординга; агента ещё нет — null, разделы пустые."""
    user = await _login(client)
    body, _ = await _export(client, user)
    assert body["agent"] is None and body["profile"]["onboarding_completed"] is False
    assert body["conversations"] == body["memory"] == body["drafts"] == []
    assert body["profile"]["partner_name"] is None


async def test_export_after_leaving_couple(
    client: httpx.AsyncClient, sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """После выхода из пары история остаётся у обоих: свои и доставленные им сообщения,
    распавшаяся пара с датами; тексты партнёра, которых он не видел, по-прежнему не видны."""
    sc = await _scene(client, sessionmaker, engine, keys)
    resp = await client.post("/api/v1/couple/leave", headers=sc.igor.headers)
    assert resp.status_code == 204
    for me, other, role in ((sc.anna, sc.igor, "initiator"), (sc.igor, sc.anna, "partner")):
        body, raw = await _export(client, me)
        [couple] = body["couples"]
        assert (couple["status"], couple["role"]) == ("dissolved", role)
        assert couple["dissolved_at"] is not None and couple["left_at"] is not None
        assert len(body["couple_messages"]["sent"]) == 4
        assert len(body["couple_messages"]["received"]) == 2
        assert other.marks["msg-plain"] in raw
        _no_marks(raw, other, only=("memory", "chat-owner", "msg-withheld", "msg-processing"))


async def test_export_after_partner_deleted_account(
    client: httpx.AsyncClient, sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """АР-23: после удаления аккаунта Игоря его сообщения удалены, а сообщения Анны остаются у
    неё (оригинал расшифровывается), версия для Игоря не выгружается."""
    sc = await _scene(client, sessionmaker, engine, keys)
    assert (await client.delete("/api/v1/me", headers=sc.igor.headers)).status_code == 204
    body, raw = await _export(client, sc.anna)
    assert body["couple_messages"]["received"] == []
    _no_marks(raw, sc.igor)
    sent = {x["original"] for x in body["couple_messages"]["sent"]}
    assert sent == {
        sc.anna.marks[f"msg-{k}"] for k in ("adapted", "plain", "withheld", "processing")
    }
    [couple] = body["couples"]
    assert couple["status"] == "dissolved"


async def test_export_fixes_original_view(
    client: httpx.AsyncClient, sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """ТЗ §7, АР-25 п.2: выдача оригинала в выгрузке фиксируется как просмотр; в ответе —
    значение после фиксации; повтор не меняет первую отметку; у автора отметки нет."""
    sc = await _scene(client, sessionmaker, engine, keys)
    msg = sc.msgs["i-adapted"][1]
    assert await _viewed_at(engine, msg) is None
    body, _ = await _export(client, sc.anna)
    stamp = await _viewed_at(engine, msg)
    got = {x["id"]: x for x in body["couple_messages"]["received"]}[str(msg)]
    assert stamp is not None and _ts(got["original_viewed_at"]) == stamp
    again, _ = await _export(client, sc.anna)
    got2 = {x["id"]: x for x in again["couple_messages"]["received"]}[str(msg)]
    assert _ts(got2["original_viewed_at"]) == stamp == await _viewed_at(engine, msg)
    for label in ("i-withheld", "i-processing", "a-adapted"):
        assert await _viewed_at(engine, sc.msgs[label][1]) is None, label
    igor, _ = await _export(client, sc.igor)
    assert "original_viewed_at" not in json.dumps(igor["couple_messages"]["sent"])


async def test_export_corrupted_rows_are_unavailable(
    client: httpx.AsyncClient,
    sessionmaker: SM,
    engine: AsyncEngine,
    keys: Keyring,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Повреждённые строки (подмена байта, перенос шифротекста в чужую строку, неизвестный
    ключ) не роняют выгрузку: `UnavailableText` с кодом причины; в журнале — только колонка и
    код, без текстов и id. Нерасшифрованный оригинал всё равно отмечен как выданный (решение
    A2: выгрузка — одна транзакция, отказ расшифровки её не откатывает)."""
    sc = await _scene(client, sessionmaker, engine, keys)
    anna = sc.anna
    received = sc.msgs["i-plain"][1]
    async with engine.begin() as conn:
        # Подмена последнего байта (тег GCM) у реплики владельца и у оригинала полученного
        # сообщения (показанная версия цела).
        for table, column, where, params in (
            (
                "conversation_messages",
                "text_enc",
                "conversation_id = :c AND author = 'owner'",
                {"c": anna.conversation_id},
            ),
            ("partner_messages", "original_enc", "id = :c", {"c": received}),
        ):
            row_id, blob = (
                await conn.execute(text(f"SELECT id, {column} FROM {table} WHERE {where}"), params)
            ).one()
            await conn.execute(
                text(f"UPDATE {table} SET {column} = :b WHERE id = :i"),
                {"i": row_id, "b": bytes(blob[:-1]) + bytes([blob[-1] ^ 1])},
            )
        # Шифротекст итога диалога переставлен в профиль агента (чужие связанные данные).
        await conn.execute(
            text(
                "UPDATE agents SET profile_summary_enc = (SELECT summary_enc FROM conversations "
                "WHERE id = :c) WHERE id = :a"
            ),
            {"c": anna.conversation_id, "a": anna.agent_id},
        )
        # Неизвестный ключ у памяти (id ключа «k1» → «zz»); не тот формат у черновика.
        await conn.execute(
            text(
                "UPDATE memory_items SET text_enc = overlay(text_enc placing CAST('zz' AS bytea) "
                "from 3 for 2) WHERE agent_id = :a"
            ),
            {"a": anna.agent_id},
        )
        await conn.execute(
            text(
                "UPDATE message_drafts SET text_enc = :b WHERE agent_id = :a AND status = 'draft'"
            ),
            {"a": anna.agent_id, "b": b"\x00\xff"},
        )
    with caplog.at_level(logging.DEBUG):
        body, raw = await _export(client, anna)
    unavailable = {"unavailable": True}
    conv = body["conversations"][0]
    owner = next(x for x in conv["messages"] if x["author"] == "owner")
    assert owner["text"] == {**unavailable, "reason": "auth"}
    agent_line = next(x for x in conv["messages"] if x["author"] == "agent")
    assert agent_line["text"] == anna.marks["chat-agent"]  # соседняя строка цела
    assert body["agent"]["profile_summary"] == {**unavailable, "reason": "auth"}
    assert body["memory"][0]["text"] == {**unavailable, "reason": "unknown_key"}
    bad_draft = [d for d in body["drafts"] if d["status"] == "draft"]
    assert [d["text"] for d in bad_draft] == [{**unavailable, "reason": "format"}]
    got = {x["id"]: x for x in body["couple_messages"]["received"]}[str(received)]
    assert got["original"] == {**unavailable, "reason": "auth"}
    assert got["text"] == sc.igor.marks["msg-plain"]
    assert await _viewed_at(engine, received) == _ts(got["original_viewed_at"])
    # Тексты повреждённых строк в ответ не попали (их и нельзя было получить).
    assert anna.marks["chat-owner"] not in raw and anna.marks["memory"] not in raw
    # Журнал: колонка и код причины — без текстов и id.
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "reason=auth" in logged and "reason=unknown_key" in logged and "reason=format" in logged
    for u in (anna, sc.igor):
        for label in u.marks.values():
            assert label not in logged
        assert str(u.id) not in logged
    for ident in (anna.conversation_id, anna.agent_id, received, sc.msgs["i-plain"][0]):
        assert str(ident) not in logged


async def test_export_lost_context_is_unavailable(
    client: httpx.AsyncClient, sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """Черновик полученного сообщения потерян (draft_id NULL, Б-38) — код `context` у
    оригинала, показанная (адаптированная) версия расшифровывается."""
    sc = await _scene(client, sessionmaker, engine, keys)
    msg = sc.msgs["i-adapted"][1]
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE partner_messages SET draft_id = NULL WHERE id = :m"), {"m": msg}
        )
    body, _ = await _export(client, sc.anna)
    got = {x["id"]: x for x in body["couple_messages"]["received"]}[str(msg)]
    assert got["original"] == {"unavailable": True, "reason": "context"}
    assert got["text"] == sc.igor.marks["msg-for-recipient"]


async def test_export_includes_safety_events(client: httpx.AsyncClient) -> None:
    """ТЗ §7, §9.3, АР-25 п.6: срабатывания — только тип и время."""
    user = await _login(client)
    body = {
        "name": "Тест",
        "address_form": "на ты",
        "partner_name": "Тестовый",
        "consent_terms": True,
        "consent_data": True,
        "consent_sensitive": True,
        "age_confirmed": False,
    }
    _error(
        await client.put("/api/v1/me/onboarding", headers=user.headers, json=body), 403, "underage"
    )
    data, _ = await _export(client, user)
    assert [e["type"] for e in data["safety_events"]] == ["underage"]


# --- Бот: уведомления после удаления --------------------------------------------------------


class _FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(json.loads(request.content or b"{}"))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


async def test_no_notification_to_deleted_user(
    client: httpx.AsyncClient, sessionmaker: SM, caplog: pytest.LogCaptureFixture
) -> None:
    """Уведомление адресуется по id пользователя, чат берётся в момент отправки: до удаления —
    уходит в его чат; после (уведомление уже было в очереди) — получатель не найден, ничего
    не отправляется, журнал пуст."""
    user = await _login(client)
    fake = _FakeTelegram()
    api = BotApi("123:fake", transport=httpx.MockTransport(fake.handler))
    try:
        assert await notify_user(api, sessionmaker, user.id, NotificationKind.NEW_MESSAGE, WEBAPP)
        assert [x["chat_id"] for x in fake.sent] == [user.tg]
        assert (await client.delete("/api/v1/me", headers=user.headers)).status_code == 204
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            sent = await notify_user(
                api, sessionmaker, user.id, NotificationKind.NEW_MESSAGE, WEBAPP
            )
        assert sent is False and len(fake.sent) == 1
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        assert not any(str(user.id) in r.getMessage() for r in caplog.records)
        # Неизвестный id — так же тихо.
        assert not await notify_user(
            api, sessionmaker, uuid.uuid4(), NotificationKind.PARTNER_JOINED, WEBAPP
        )
    finally:
        await api.aclose()

"""Б-84 (A2): причина задержки сообщения в API ленты — `PartnerMessage.withheld_reason` и
`withheld_crisis` (контракт; АР-44, АР-45; ТЗ §9.3).

Дополняет `test_couple_messages_api.py` (там — остановки реальными функциями воркера):
- каждое значение CHECK `withheld_reason_code` (включая `couple_ended`, которое лента текущей пары
  в жизни не показывает — состояние проставлено напрямую, проверяется только отображение) и
  каждый тип кризиса доходят до автора и только до него;
- получатель и посторонний не получают ни сообщения, ни полей; оригинал и «прочитано»
  остановленного — 404 (неразличимо с несуществующим);
- перечисления в схеме ответа совпадают с контрактом и с CHECK/типом БД.

Данные выдуманные; тексты помечены метками — утечки ищутся поиском меток."""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from app.api.couple_messages import PartnerMessage
from tests import test_couple_messages_api as base
from tests.contract import SPEC, assert_matches
from tests.test_couple_messages_api import (
    FEED,
    Env,
    U,
    _error,
    _pair,
    _person,
    deliver,
    feed,
    send,
)

env = base.env  # фикстура ленты (клиент, ключи, БД)

REASONS = ("safety", "model_refusal", "pipeline_error", "attempts_exhausted", "couple_ended")
CRISES = ("violence", "threat_outgoing", "self_harm", "life_threat")
CASES = [("safety", c) for c in (*CRISES, None)] + [(r, None) for r in REASONS[1:]]


def _contract_enum(field: str) -> set[str]:
    prop = SPEC["components"]["schemas"]["PartnerMessage"]["properties"][field]
    return {v for v in prop["enum"] if v is not None}


def _model_enum(field: str) -> set[str]:
    schema = PartnerMessage.model_json_schema()["properties"][field]
    values: set[str] = set()
    for option in schema.get("anyOf", [schema]):
        values.update(v for v in option.get("enum", []) if v is not None)
    return values


async def _stop(env: Env, msg: uuid.UUID, reason: str, crisis: str | None) -> None:
    """Исход остановки, как его записывают функции БД (CHECK 0013/0014 действуют): только
    из ``processing``, без событий — проверяется отображение, а не конвейер."""
    async with env.engine.connect() as conn, conn.begin():
        done = await conn.execute(
            text(
                "UPDATE partner_messages SET status = 'withheld', withheld_reason = :r, "
                "withheld_crisis = CAST(:c AS safety_event_type) "
                "WHERE id = :m AND status = 'processing'"
            ),
            {"m": msg, "r": reason, "c": crisis},
        )
    assert done.rowcount == 1


async def _trio(env: Env) -> tuple[U, U, U]:
    anna, igor = await _person(env, "Анна", "Игорь"), await _person(env, "Игорь", "Анна")
    olga = await _person(env, "Ольга", "Пётр")
    await _pair(env, anna, igor)
    return anna, igor, olga


# --------------------------------------------------------------------------- контракт


def test_response_enums_match_contract() -> None:
    """Схема ответа (pydantic) объявляет те же значения, что контракт, — ни больше, ни меньше;
    оба поля nullable."""
    assert _model_enum("withheld_reason") == _contract_enum("withheld_reason") == set(REASONS)
    assert _model_enum("withheld_crisis") == _contract_enum("withheld_crisis") == set(CRISES)
    props = SPEC["components"]["schemas"]["PartnerMessage"]["properties"]
    for name in ("withheld_reason", "withheld_crisis"):
        assert None in props[name]["enum"] and "null" in props[name]["type"]
        assert name not in SPEC["components"]["schemas"]["PartnerMessage"]["required"]


async def test_contract_enums_match_database(env: Env) -> None:
    """Каждое значение CHECK ``withheld_reason_code`` есть в контракте (иначе ответ упал бы на
    проверке схемы); типы кризиса контракта — значения ``safety_event_type`` без ``underage``
    (CHECK ``withheld_crisis_safety``)."""
    async with env.engine.connect() as conn:
        check = (
            await conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'partner_messages'::regclass "
                    "AND conname = 'ck_partner_messages_withheld_reason_code'"
                )
            )
        ).scalar_one()
        types = (
            await conn.execute(text("SELECT unnest(enum_range(NULL::safety_event_type))::text"))
        ).scalars()
        crisis_types = set(types) - {"underage"}
    assert set(re.findall(r"'([a-z_]+)'", check)) == set(REASONS)
    assert crisis_types == set(CRISES)


# --------------------------------------------------------------------------- автор / получатель


@pytest.mark.parametrize(("reason", "crisis"), CASES)
async def test_author_sees_each_reason_recipient_and_outsider_do_not(
    env: Env, reason: str, crisis: str | None
) -> None:
    """Автору — причина (и тип кризиса только при ``safety``), текст сохранён у автора; ответ
    по схеме ``PartnerMessage``. Получателю и посторонней — ни сообщения, ни полей; оригинал и
    «прочитано» остановленного — 404, как у несуществующего."""
    anna, igor, olga = await _trio(env)
    body = env.mark(f"stop-{reason}-{crisis}")
    mid = await send(env, anna, body)
    shown = env.mark("shown")
    ok = await send(env, anna, env.mark("ok"))
    await deliver(env, ok, shown, adapted=False)
    await _stop(env, mid, reason, crisis)

    mine = {i["id"]: i for i in (await feed(env, anna))["items"]}
    item = mine[str(mid)]
    assert_matches(item, "PartnerMessage")
    assert (item["status"], item["withheld_reason"], item["withheld_crisis"]) == (
        "withheld",
        reason,
        crisis,
    )
    assert item["direction"] == "outgoing" and item["text"] == body
    assert (mine[str(ok)]["withheld_reason"], mine[str(ok)]["withheld_crisis"]) == (None, None)

    got = (await feed(env, igor))["items"]
    assert [i["id"] for i in got] == [str(ok)]
    assert (got[0]["withheld_reason"], got[0]["withheld_crisis"]) == (None, None)
    assert body not in str(got)
    assert (await feed(env, olga))["items"] == []

    for who in (igor, olga):
        _error(
            await env.client.get(f"{FEED}/{mid}/original", headers=who.headers), 404, "not_found"
        )
        _error(await env.client.post(f"{FEED}/{mid}/read", headers=who.headers), 404, "not_found")
    _error(await env.client.get(f"{FEED}/{ok}/original", headers=olga.headers), 404, "not_found")
    # Автору свой оригинал остановленного — доступен (текст сохранён, «отправить снова»).
    resp = await env.client.get(f"{FEED}/{mid}/original", headers=anna.headers)
    assert resp.status_code == 200 and resp.json()["text"] == body


async def test_forged_ids_are_404(env: Env) -> None:
    """Подделанный id (несуществующий, не-uuid) — 404, неразличимо с чужим остановленным."""
    anna, _igor, _olga = await _trio(env)
    for fake in (uuid.uuid4(), "not-a-uuid"):
        _error(
            await env.client.get(f"{FEED}/{fake}/original", headers=anna.headers), 404, "not_found"
        )
        _error(await env.client.post(f"{FEED}/{fake}/read", headers=anna.headers), 404, "not_found")


async def test_couple_ended_withheld_not_in_feed_after_leave(env: Env) -> None:
    """``couple_ended`` в жизни ставится после распада пары: лента текущей пары пуста у обоих
    (ТЗ §4.5 п.2), оригинал бывшей пары — 404 и автору."""
    anna, igor, _olga = await _trio(env)
    mid = await send(env, anna, env.mark("ended"))
    assert (await env.client.post("/api/v1/couple/leave", headers=igor.headers)).status_code == 204
    await _stop(env, mid, "couple_ended", None)
    for who in (anna, igor):
        assert (await feed(env, who))["items"] == []
    _error(await env.client.get(f"{FEED}/{mid}/original", headers=anna.headers), 404, "not_found")


async def test_withheld_fields_logs_have_no_texts(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    anna, igor, olga = await _trio(env)
    ids: list[Any] = []
    for reason, crisis in CASES:
        mid = await send(env, anna, env.mark(f"log-{reason}-{crisis}"))
        await _stop(env, mid, reason, crisis)
        ids.append(mid)
    for who in (anna, igor, olga):
        await feed(env, who)
        await env.client.get(f"{FEED}/{ids[0]}/original", headers=who.headers)
    # Журнал тестового клиента httpx (URL с id) — не журнал приложения.
    records = [r for r in caplog.records if not r.name.startswith("httpx")]
    logged = "\n".join(f"{r.getMessage()} {r.args!r}" for r in records)
    for label in env.marks:
        assert label not in logged and label.split("-")[-1] not in logged
    assert str(anna.id) not in logged and all(str(i) not in logged for i in ids)

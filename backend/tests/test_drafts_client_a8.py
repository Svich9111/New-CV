"""3.4 (A8, раунд 1): запросы клиента черновика и ленты — ровно в том виде, в каком их шлёт
`frontend/src/api/client.ts`, — на настоящем сервере.

ФТ-07, ФТ-08, ФТ-10, ФТ-11; ТЗ §2 п.2, §6.2 п.2. Клиент шлёт `POST /drafts` и
`POST /drafts/{id}/regenerate` с телом `{}` (не без тела), ленту — `?limit=30`, «Отправить» —
`{expected_version}` показанной версии. Двойное нажатие на двух устройствах (две одновременные
отправки одной версии) даёт одно сообщение партнёру. Данные выдуманные, модель — заглушка."""

from __future__ import annotations

import asyncio

from tests.contract import assert_matches
from tests.test_drafts_api import (  # noqa: F401 — фикстура env
    DRAFTS,
    Env,
    Model,
    _error,
    couple,
    env,
    ok_draft,
    partner_messages,
)

FEED = "/api/v1/couple/messages"


async def test_client_bodies_empty_object_accepted(env: Env) -> None:  # noqa: F811
    """`{}` без `reply_to_id` — как у клиента; `regenerate` с `{}` — новая версия; send не вызван —
    сообщений пары нет (ФТ-08)."""
    anna, _igor = await couple(env)
    env.use(Model("Мне тревожно. Обсудим траты?", "Мне важно обсуждать траты заранее."))
    before = await partner_messages(env)
    d = ok_draft(await env.client.post(DRAFTS, headers=anna.headers, json={}), 201)
    assert d["reply_to_id"] is None and d["version"] == 1
    r = ok_draft(
        await env.client.post(f"{DRAFTS}/{d['id']}/regenerate", headers=anna.headers, json={}), 200
    )
    assert r["version"] == 2
    assert await partner_messages(env) == before


async def test_double_send_same_version_one_message(env: Env) -> None:  # noqa: F811
    """Два одновременных «Отправить» одной версии (два устройства) — одно 202, второе 409; одно
    сообщение партнёру."""
    anna, _igor = await couple(env)
    before = await partner_messages(env)
    d = ok_draft(await env.client.post(DRAFTS, headers=anna.headers, json={}), 201)
    url = f"{DRAFTS}/{d['id']}/send"
    a, b = await asyncio.gather(
        env.client.post(url, headers=anna.headers, json={"expected_version": 1}),
        env.client.post(url, headers=anna.headers, json={"expected_version": 1}),
    )
    codes = sorted([a.status_code, b.status_code])
    assert codes == [202, 409], (a.text, b.text)
    loser = a if a.status_code == 409 else b
    assert loser.json()["code"] in {"not_editable", "conflict_retry", "version_conflict"}
    assert await partner_messages(env) == before + 1
    # Повтор после «ответ потерян» (клиент нажимает ещё раз той же версией) — not_editable.
    _error(
        await env.client.post(url, headers=anna.headers, json={"expected_version": 1}),
        409,
        "not_editable",
    )
    assert await partner_messages(env) == before + 1


async def test_feed_client_query_and_own_message_no_read_no_view(env: Env) -> None:  # noqa: F811
    """Лента `?limit=30` по контракту; своё отправленное автор не отмечает прочитанным (404), а
    получатель его в `processing` не видит (ни в ленте, ни через `/original`, ни `/read`)."""
    anna, igor = await couple(env)
    d = ok_draft(await env.client.post(DRAFTS, headers=anna.headers, json={}), 201)
    sent = await env.client.post(
        f"{DRAFTS}/{d['id']}/send", headers=anna.headers, json={"expected_version": 1}
    )
    assert sent.status_code == 202, sent.text
    mid = sent.json()["id"]
    page = await env.client.get(f"{FEED}?limit=30", headers=anna.headers)
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert_matches(page.json()["items"][0], "PartnerMessage")
    assert [i["id"] for i in page.json()["items"]] == [mid]
    assert (await env.client.post(f"{FEED}/{mid}/read", headers=anna.headers)).status_code == 404
    assert (await env.client.get(f"{FEED}?limit=30", headers=igor.headers)).json()["items"] == []
    assert (await env.client.get(f"{FEED}/{mid}/original", headers=igor.headers)).status_code == 404
    assert (await env.client.post(f"{FEED}/{mid}/read", headers=igor.headers)).status_code == 404
    # Неверный курсор — 422 (клиент начинает с первой страницы, курсор не повторяет).
    assert (
        await env.client.get(f"{FEED}?limit=30&cursor=bogus", headers=anna.headers)
    ).status_code == 422

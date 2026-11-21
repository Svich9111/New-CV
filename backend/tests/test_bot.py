"""1.4 / ФТ-03, ФТ-18: Telegram-бот — вебхук, /start с кнопкой Mini App, идемпотентность по
update_id, клиент Bot API (таймауты, 429, токен вне журналов и ошибок). Сети нет: Bot API
подменён httpx.MockTransport. Данные — выдуманные."""

import asyncio
import inspect
import json
import logging
import os
import signal
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.bot import check as check_module
from app.bot import poll as poll_module
from app.bot import setup as bot_setup
from app.bot import webhook as webhook_module
from app.bot.client import BotApi, BotApiError
from app.bot.dedup import UpdateDeduplicator
from app.bot.handlers import HINT_TEXT, START_TEXT
from app.bot.notifications import NotificationKind, notify
from app.bot.setup import configure_bot
from app.config import (
    ConfigError,
    Settings,
    check_bot_config,
    check_dev_bot_config,
    get_settings,
)
from app.main import app

TOKEN = "123456:TEST-bot-token-not-real-AAAAAAAA"
SECRET = "s" * 40
WEBHOOK_SECRET = "Wh_secret-" + "x" * 30
WEBAPP_URL = "https://app.example.test/"
URL = "/api/v1/telegram/webhook"
PAYLOAD = "PRIVATE_PAYLOAD_Zx9_do_not_log"


def _settings(webhook_secret: str = WEBHOOK_SECRET) -> Settings:
    return Settings(
        telegram_bot_token=SecretStr(TOKEN),
        session_secret=SecretStr(SECRET),
        bot_username="FeelingsTestBot",
        telegram_webhook_secret=SecretStr(webhook_secret),
        webapp_url=WEBAPP_URL,
    )


class FakeTelegram:
    """Подменённый Bot API: записывает вызовы, отвечает по очереди заданными ответами."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.raw_urls: list[str] = []
        self.responses: list[httpx.Response | Exception] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.raw_urls.append(str(request.url))
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((method, json.loads(request.content or b"{}")))
        if self.responses:
            nxt = self.responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    def sent(self) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == "sendMessage"]


def _api(fake: FakeTelegram, sleeps: list[float] | None = None, **kw: Any) -> BotApi:
    async def _sleep(s: float) -> None:
        if sleeps is not None:
            sleeps.append(s)

    return BotApi(TOKEN, transport=httpx.MockTransport(fake.handler), sleep=_sleep, **kw)


@pytest.fixture
def fake() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
async def client(fake: FakeTelegram) -> AsyncIterator[httpx.AsyncClient]:
    saved = (getattr(app.state, "bot_api", None), getattr(app.state, "bot_updates", None))
    app.state.bot_api = _api(fake)
    app.state.bot_updates = UpdateDeduplicator()
    app.dependency_overrides[get_settings] = _settings
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    await app.state.bot_api.aclose()
    app.state.bot_api, app.state.bot_updates = saved


_next_id = [700_000]


def _uid() -> int:
    _next_id[0] += 1
    return _next_id[0]


def _message(text: str | None, chat_type: str = "private", update_id: int | None = None) -> dict:
    msg: dict[str, Any] = {
        "message_id": 10,
        "date": 1_700_000_000,
        "chat": {"id": 42_000_001, "type": chat_type},
        "from": {"id": 42_000_001, "is_bot": False, "first_name": "Анна"},
    }
    if text is not None:
        msg["text"] = text
    return {"update_id": update_id or _uid(), "message": msg}


def _hdr(secret: str = WEBHOOK_SECRET) -> dict[str, str]:
    return {"X-Telegram-Bot-Api-Secret-Token": secret}


def _assert_webapp_button(sent: dict[str, Any]) -> None:
    rows = sent["reply_markup"]["inline_keyboard"]
    buttons = [b for row in rows for b in row]
    assert any(b.get("web_app") == {"url": WEBAPP_URL} for b in buttons), sent


# --- Секретный заголовок ---------------------------------------------------------------------


async def test_webhook_without_secret_header_is_401(
    client: httpx.AsyncClient, fake: FakeTelegram
) -> None:
    resp = await client.post(URL, json=_message("/start"))
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"
    assert fake.calls == []


@pytest.mark.parametrize(
    "bad", ["wrong", WEBHOOK_SECRET[:-1], WEBHOOK_SECRET + "x", WEBHOOK_SECRET.upper(), " "]
)
async def test_webhook_with_wrong_secret_is_403(
    client: httpx.AsyncClient, fake: FakeTelegram, bad: str
) -> None:
    resp = await client.post(URL, json=_message("/start"), headers=_hdr(bad))
    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden"
    assert fake.calls == []


async def test_webhook_empty_header_is_rejected(
    client: httpx.AsyncClient, fake: FakeTelegram
) -> None:
    resp = await client.post(URL, json=_message("/start"), headers=_hdr(""))
    assert resp.status_code == 403  # заголовок есть, но не совпадает (ревью 1.4 m7)
    assert fake.calls == []


async def test_webhook_rejects_everything_when_secret_not_configured(
    client: httpx.AsyncClient, fake: FakeTelegram
) -> None:
    """Пустой секрет в настройках не должен совпадать с пустым заголовком."""
    app.dependency_overrides[get_settings] = lambda: _settings(webhook_secret="")
    for headers in ({}, _hdr("")):
        resp = await client.post(URL, json=_message("/start"), headers=headers)
        assert resp.status_code == 503
        assert resp.json()["code"] == "service_unavailable"
    assert fake.calls == []


async def test_secret_compared_in_constant_time(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = webhook_module.hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(webhook_module.hmac, "compare_digest", spy)
    resp = await client.post(URL, json=_message("/start"), headers=_hdr("wrong"))
    assert resp.status_code == 403
    assert calls, "секрет сравнивается не через hmac.compare_digest"


async def test_secret_checked_before_body_is_parsed(
    client: httpx.AsyncClient, fake: FakeTelegram
) -> None:
    resp = await client.post(
        URL, content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 401
    assert fake.calls == []


# --- /start -----------------------------------------------------------------------------------


async def test_start_replies_with_mini_app_button(
    client: httpx.AsyncClient, fake: FakeTelegram
) -> None:
    resp = await client.post(URL, json=_message("/start"), headers=_hdr())
    assert resp.status_code == 200
    sent = fake.sent()
    assert len(sent) == 1
    assert sent[0]["chat_id"] == 42_000_001
    assert sent[0]["text"]
    _assert_webapp_button(sent[0])


@pytest.mark.parametrize(
    "text",
    [
        f"/start {PAYLOAD}",
        f"/start@FeelingsTestBot {PAYLOAD}",
        f"/start   {PAYLOAD} ещё слова",
        "/start " + "я" * 4000,
        "/START",
    ],
)
async def test_start_with_any_payload_replies_and_payload_not_logged(
    client: httpx.AsyncClient, fake: FakeTelegram, caplog: pytest.LogCaptureFixture, text: str
) -> None:
    caplog.set_level(logging.DEBUG)
    resp = await client.post(URL, json=_message(text), headers=_hdr())
    assert resp.status_code == 200
    sent = fake.sent()
    assert len(sent) == 1
    _assert_webapp_button(sent[0])
    # payload не возвращается в ответе и не уходит в Bot API обратно
    assert PAYLOAD not in sent[0]["text"]
    assert PAYLOAD not in json.dumps(sent[0], ensure_ascii=False)
    assert PAYLOAD not in caplog.text
    assert "я" * 50 not in caplog.text
    assert TOKEN not in caplog.text


async def test_help_command_also_offers_button(
    client: httpx.AsyncClient, fake: FakeTelegram
) -> None:
    resp = await client.post(URL, json=_message("/help"), headers=_hdr())
    assert resp.status_code == 200
    assert len(fake.sent()) == 1
    _assert_webapp_button(fake.sent()[0])


async def test_webhook_logs_contain_no_message_text(
    client: httpx.AsyncClient, fake: FakeTelegram, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    secret_text = "ЛИЧНОЕ_ПРИЗНАНИЕ_Qw7 он опять не пришёл"
    for text in (secret_text, f"/unknown {secret_text}"):
        resp = await client.post(URL, json=_message(text), headers=_hdr())
        assert resp.status_code == 200
    assert "ЛИЧНОЕ_ПРИЗНАНИЕ_Qw7" not in caplog.text
    assert "/unknown" not in caplog.text
    for sent in fake.sent():
        assert "ЛИЧНОЕ_ПРИЗНАНИЕ_Qw7" not in sent["text"]


# --- Идемпотентность --------------------------------------------------------------------------


async def test_repeated_update_id_does_not_reply_twice(
    client: httpx.AsyncClient, fake: FakeTelegram
) -> None:
    update = _message(f"/start {PAYLOAD}")
    for _ in range(3):
        resp = await client.post(URL, json=update, headers=_hdr())
        assert resp.status_code == 200
    assert len(fake.sent()) == 1


async def test_different_update_ids_each_get_reply(
    client: httpx.AsyncClient, fake: FakeTelegram
) -> None:
    for _ in range(3):
        await client.post(URL, json=_message("/start"), headers=_hdr())
    assert len(fake.sent()) == 3


async def test_transient_bot_api_failure_allows_redelivery(
    client: httpx.AsyncClient, fake: FakeTelegram
) -> None:
    """Сбой Bot API (5xx/сеть) — 503, Telegram доставит повторно, и ответ уйдёт один раз."""
    update = _message("/start")
    fake.responses = [httpx.Response(502, json={"ok": False, "error_code": 502})]
    first = await client.post(URL, json=update, headers=_hdr())
    assert first.status_code == 503
    second = await client.post(URL, json=update, headers=_hdr())
    assert second.status_code == 200
    third = await client.post(URL, json=update, headers=_hdr())
    assert third.status_code == 200
    assert len(fake.sent()) == 2  # неудачная попытка + одна успешная, без третьей


async def test_timeout_is_transient(client: httpx.AsyncClient, fake: FakeTelegram) -> None:
    update = _message("/start")
    fake.responses = [httpx.ReadTimeout("timed out")]
    assert (await client.post(URL, json=update, headers=_hdr())).status_code == 503
    assert (await client.post(URL, json=update, headers=_hdr())).status_code == 200


async def test_permanent_bot_api_error_is_not_redelivered(
    client: httpx.AsyncClient, fake: FakeTelegram
) -> None:
    """Пользователь заблокировал бота (403) — повтор бесполезен: 200, повторная доставка
    не вызывает Bot API снова."""
    update = _message("/start")
    fake.responses = [
        httpx.Response(
            403,
            json={"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked"},
        )
    ]
    assert (await client.post(URL, json=update, headers=_hdr())).status_code == 200
    assert (await client.post(URL, json=update, headers=_hdr())).status_code == 200
    assert len(fake.calls) == 1


async def test_429_through_webhook_is_503_and_redelivery_answered(
    client: httpx.AsyncClient, fake: FakeTelegram
) -> None:
    """Ревью 1.4 m8: 429 с долгим retry_after — 503, отметка снята, повтор получает ответ."""
    update = _message("/start")
    fake.responses = [
        httpx.Response(
            429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 3600}}
        )
    ]
    assert (await client.post(URL, json=update, headers=_hdr())).status_code == 503
    assert (await client.post(URL, json=update, headers=_hdr())).status_code == 200
    assert (await client.post(URL, json=update, headers=_hdr())).status_code == 200
    assert len(fake.sent()) == 2


async def test_connect_error_through_webhook_is_transient(
    client: httpx.AsyncClient, fake: FakeTelegram, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    update = _message("/start")
    fake.responses = [httpx.ConnectError(f"connect https://api.telegram.org/bot{TOKEN}/x")]
    assert (await client.post(URL, json=update, headers=_hdr())).status_code == 503
    assert (await client.post(URL, json=update, headers=_hdr())).status_code == 200
    assert len(fake.sent()) == 2
    assert TOKEN not in caplog.text


async def test_unexpected_handler_error_is_200_without_text_or_traceback(
    client: httpx.AsyncClient,
    fake: FakeTelegram,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ревью 1.4 m8: ветка except Exception — 200, отметка остаётся, в журнале нет текста
    исключения и трассировки."""
    caplog.set_level(logging.DEBUG)
    calls: list[int] = []

    async def boom(api: BotApi, update: dict[str, Any], webapp_url: str) -> str:
        calls.append(update["update_id"])
        raise RuntimeError("ЛИЧНОЕ_В_ИСКЛЮЧЕНИИ_Rt5")

    monkeypatch.setattr(webhook_module, "handle_update", boom)
    update = _message("/start")
    assert (await client.post(URL, json=update, headers=_hdr())).status_code == 200
    assert (await client.post(URL, json=update, headers=_hdr())).status_code == 200
    assert len(calls) == 1  # повтор — дубль, обработчик не вызывается снова
    assert "ЛИЧНОЕ_В_ИСКЛЮЧЕНИИ_Rt5" not in caplog.text
    assert "Traceback" not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)
    assert "handler failed" in caplog.text


async def test_redelivery_while_first_in_progress_waits_for_outcome(
    client: httpx.AsyncClient, fake: FakeTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A8 D-2 / ревью m2: повтор во время первой доставки не подтверждается, пока та не
    завершена (по сроку — 503), а после её завершения — 200 без второго ответа."""
    monkeypatch.setattr(webhook_module, "HANDLE_DEADLINE_S", 0.05)
    updates: UpdateDeduplicator = app.state.bot_updates
    update = _message("/start")
    uid = update["update_id"]
    assert updates.claim(uid)  # первая доставка «в работе»
    assert (await client.post(URL, json=update, headers=_hdr())).status_code == 503
    assert updates.in_progress(uid)  # повтор не снял чужую отметку
    updates.complete(uid)
    assert (await client.post(URL, json=update, headers=_hdr())).status_code == 200
    assert fake.sent() == []


async def test_waiting_duplicate_mirrors_first_outcome() -> None:
    import asyncio

    d = UpdateDeduplicator()
    assert d.claim(1) and d.claim(2)

    async def finish() -> None:
        await asyncio.sleep(0.01)
        d.complete(1)
        d.release(2)

    ok, failed, _ = await asyncio.gather(d.wait_outcome(1), d.wait_outcome(2), finish())
    assert ok is True
    assert failed is False
    assert await d.wait_outcome(3) is False  # неизвестное — не подтверждается


async def test_processing_deadline_gives_503_and_allows_redelivery(
    client: httpx.AsyncClient, fake: FakeTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ревью m2: общий срок обработки ограничен; по истечении — 503, отметка снята."""
    import asyncio

    hang = asyncio.Event()

    async def slow(api: BotApi, update: dict[str, Any], webapp_url: str) -> str:
        await hang.wait()
        return "start"

    monkeypatch.setattr(webhook_module, "HANDLE_DEADLINE_S", 0.05)
    monkeypatch.setattr(webhook_module, "handle_update", slow)
    update = _message("/start")
    assert (await client.post(URL, json=update, headers=_hdr())).status_code == 503
    assert not app.state.bot_updates.in_progress(update["update_id"])
    monkeypatch.undo()
    assert (await client.post(URL, json=update, headers=_hdr())).status_code == 200
    assert len(fake.sent()) == 1


async def test_cancelled_handler_releases_update(
    client: httpx.AsyncClient, fake: FakeTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A8 1.4 D-3: отмена обработчика (CancelledError — не Exception) снимает отметку и не
    глушится; ожидающий повтор получает 503 сразу, следующая доставка обрабатывается."""
    import asyncio

    started = asyncio.Event()

    async def hang(api: BotApi, update: dict[str, Any], webapp_url: str) -> str:
        started.set()
        await asyncio.Event().wait()
        return "start"

    monkeypatch.setattr(webhook_module, "handle_update", hang)
    update = _message("/start")
    uid = update["update_id"]
    first = asyncio.create_task(client.post(URL, json=update, headers=_hdr()))
    await asyncio.wait_for(started.wait(), 5.0)
    waiting = asyncio.create_task(client.post(URL, json=update, headers=_hdr()))
    await asyncio.sleep(0.01)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not app.state.bot_updates.in_progress(uid)
    # повтор, ждавший исхода, не дожидается срока (20 с) и не подтверждается
    assert (await asyncio.wait_for(waiting, 5.0)).status_code == 503
    monkeypatch.undo()
    assert (await client.post(URL, json=update, headers=_hdr())).status_code == 200
    assert len(fake.sent()) == 1


def test_deadline_is_well_below_telegram_wait() -> None:
    assert 0 < webhook_module.HANDLE_DEADLINE_S <= 30


def test_deduplicator_tracks_progress() -> None:
    d = UpdateDeduplicator()
    assert d.claim(1)
    assert d.in_progress(1)
    d.complete(1)
    assert not d.in_progress(1)
    assert not d.claim(1)
    d.complete(99)  # неизвестный — без ошибки и без записи
    assert len(d) == 1


def test_deduplicator_is_bounded() -> None:
    d = UpdateDeduplicator(max_size=3)
    assert all(d.claim(i) for i in (1, 2, 3))
    assert not d.claim(2)
    assert d.claim(4)  # вытесняет самый старый (1)
    assert len(d) == 3
    assert d.claim(1)
    d.release(1)
    assert d.claim(1)


# --- Неизвестное не роняет обработчик --------------------------------------------------------


@pytest.mark.parametrize(
    "update",
    [
        {"update_id": 1, "edited_message": {"message_id": 1, "chat": {"id": 1, "type": "private"}}},
        {"update_id": 2, "callback_query": {"id": "x", "from": {"id": 1}}},
        {"update_id": 3, "my_chat_member": {"chat": {"id": 1, "type": "private"}}},
        {"update_id": 4, "pre_checkout_query": {"id": "q"}},
        {"update_id": 5},
        {"update_id": 6, "message": None},
        {"update_id": 7, "message": "строка"},
        {"update_id": 8, "message": {"chat": "не объект", "text": "/start"}},
        {"update_id": 9, "message": {"chat": {"id": "abc", "type": "private"}, "text": "/start"}},
        {"update_id": 10, "message": {"chat": {"id": 1, "type": "private"}, "text": 123}},
        {"update_id": "11", "message": {"chat": {"id": 1, "type": "private"}, "text": "/start"}},
        {"update_id": True, "message": {"chat": {"id": 1, "type": "private"}, "text": "/start"}},
        {"message": {"chat": {"id": 1, "type": "private"}, "text": "/start"}},
        [],
        "строка",
        None,
        12,
    ],
)
async def test_unknown_or_malformed_updates_do_not_crash(
    client: httpx.AsyncClient, fake: FakeTelegram, update: Any
) -> None:
    resp = await client.post(URL, json=update, headers=_hdr())
    assert resp.status_code == 200
    assert fake.sent() == []


async def test_invalid_json_with_valid_secret_is_ignored(
    client: httpx.AsyncClient, fake: FakeTelegram
) -> None:
    resp = await client.post(
        URL, content=b"{not json", headers={**_hdr(), "Content-Type": "application/json"}
    )
    assert resp.status_code == 200
    assert fake.calls == []


@pytest.mark.parametrize("text", ["/unknown", "/settings arg", "просто текст", "/"])
async def test_unknown_commands_and_text_get_hint_without_echo(
    client: httpx.AsyncClient, fake: FakeTelegram, text: str
) -> None:
    resp = await client.post(URL, json=_message(text), headers=_hdr())
    assert resp.status_code == 200
    sent = fake.sent()
    assert len(sent) == 1
    _assert_webapp_button(sent[0])
    assert text not in sent[0]["text"] or text == "/"


async def test_message_without_text_does_not_crash(
    client: httpx.AsyncClient, fake: FakeTelegram
) -> None:
    resp = await client.post(URL, json=_message(None), headers=_hdr())
    assert resp.status_code == 200


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
async def test_bot_does_not_answer_outside_private_chats(
    client: httpx.AsyncClient, fake: FakeTelegram, chat_type: str
) -> None:
    resp = await client.post(URL, json=_message("/start", chat_type=chat_type), headers=_hdr())
    assert resp.status_code == 200
    assert fake.sent() == []


async def test_webhook_unavailable_without_bot_client(client: httpx.AsyncClient) -> None:
    saved = app.state.bot_api
    app.state.bot_api = None
    try:
        resp = await client.post(URL, json=_message("/start"), headers=_hdr())
        assert resp.status_code == 503
    finally:
        app.state.bot_api = saved


# --- Настройки и старт сервиса ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("telegram_bot_token", SecretStr("")),
        ("telegram_webhook_secret", SecretStr("")),
        ("telegram_webhook_secret", SecretStr("short")),
        ("telegram_webhook_secret", SecretStr("x" * 31)),
        ("telegram_webhook_secret", SecretStr("x" * 257)),
        ("telegram_webhook_secret", SecretStr("пароль" * 10)),
        ("telegram_webhook_secret", SecretStr("a b" * 20)),
        ("webapp_url", ""),
        ("webapp_url", "http://app.example.test/"),
        ("webapp_url", "app.example.test"),
        ("webapp_url", "https://"),
    ],
)
def test_bot_config_rejects_bad_values(field: str, value: Any) -> None:
    settings = _settings().model_copy(update={field: value})
    with pytest.raises(ConfigError) as exc:
        check_bot_config(settings)
    assert TOKEN not in str(exc.value)
    assert WEBHOOK_SECRET not in str(exc.value)


def test_bot_config_accepts_good_values() -> None:
    check_bot_config(_settings())
    check_bot_config(_settings(webhook_secret="A" * 256))


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    env = {
        "FEELINGS_TELEGRAM_BOT_TOKEN": TOKEN,
        "FEELINGS_SESSION_SECRET": SECRET,
        "FEELINGS_BOT_USERNAME": "FeelingsTestBot",
        "FEELINGS_TELEGRAM_WEBHOOK_SECRET": WEBHOOK_SECRET,
        "FEELINGS_WEBAPP_URL": WEBAPP_URL,
    }
    env.update(overrides)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


@pytest.mark.parametrize(
    "missing",
    ["FEELINGS_TELEGRAM_BOT_TOKEN", "FEELINGS_TELEGRAM_WEBHOOK_SECRET", "FEELINGS_WEBAPP_URL"],
)
async def test_service_does_not_start_without_bot_settings(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    from app.main import lifespan

    _set_env(monkeypatch, **{missing: ""})
    saved = app.state.sessionmaker, app.state.bot_api
    app.state.sessionmaker = None
    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigError):
            async with lifespan(app):
                pytest.fail("сервис запустился без настроек бота")
        assert app.state.sessionmaker is None
    finally:
        get_settings.cache_clear()
        app.state.sessionmaker, app.state.bot_api = saved


async def test_service_starts_with_bot_settings_and_creates_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import lifespan

    _set_env(monkeypatch)
    saved = app.state.sessionmaker, app.state.bot_api
    app.state.sessionmaker = None
    app.state.bot_api = None
    get_settings.cache_clear()
    try:
        async with lifespan(app):
            assert isinstance(app.state.bot_api, BotApi)
            assert TOKEN not in repr(app.state.bot_api)
    finally:
        get_settings.cache_clear()
        app.state.sessionmaker, app.state.bot_api = saved


async def test_lifespan_closes_resources_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ревью 1.4 m9: при аварийном выходе клиент Bot API закрывается."""
    from app.main import lifespan

    _set_env(monkeypatch)
    saved = app.state.sessionmaker, app.state.bot_api
    app.state.sessionmaker = None
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError):
            async with lifespan(app):
                api = app.state.bot_api
                raise RuntimeError("авария")
        assert api._http.is_closed
    finally:
        get_settings.cache_clear()
        app.state.sessionmaker, app.state.bot_api = saved


def test_env_example_lists_bot_settings() -> None:
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
    assert "FEELINGS_TELEGRAM_WEBHOOK_SECRET=\n" in text
    assert "FEELINGS_WEBAPP_URL=" in text


# --- Клиент Bot API ---------------------------------------------------------------------------


async def test_client_has_timeouts() -> None:
    api = BotApi(TOKEN)
    try:
        t = api.timeout
        assert t.connect is not None and t.read is not None and t.write is not None
        assert 0 < t.read <= 30
    finally:
        await api.aclose()


async def test_client_retries_429_with_retry_after(fake: FakeTelegram) -> None:
    sleeps: list[float] = []
    fake.responses = [
        httpx.Response(
            429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 3}}
        ),
        httpx.Response(
            429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}
        ),
    ]
    api = _api(fake, sleeps)
    result = await api.send_message(1, "Проверка")
    await api.aclose()
    assert result == {"message_id": 1}
    assert sleeps == [3, 1]
    assert len(fake.sent()) == 3


async def test_client_gives_up_after_max_retries(fake: FakeTelegram) -> None:
    sleeps: list[float] = []
    resp = {"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}
    fake.responses = [httpx.Response(429, json=resp) for _ in range(10)]
    api = _api(fake, sleeps, max_retries=2)
    with pytest.raises(BotApiError) as exc:
        await api.send_message(1, "Проверка")
    await api.aclose()
    assert exc.value.error_code == 429
    assert exc.value.transient
    assert len(sleeps) == 2
    assert len(fake.calls) == 3


async def test_client_does_not_sleep_on_too_long_retry_after(fake: FakeTelegram) -> None:
    sleeps: list[float] = []
    resp = {"ok": False, "error_code": 429, "parameters": {"retry_after": 3600}}
    fake.responses = [httpx.Response(429, json=resp)]
    api = _api(fake, sleeps)
    with pytest.raises(BotApiError) as exc:
        await api.send_message(1, "Проверка")
    await api.aclose()
    assert sleeps == []
    assert exc.value.retry_after == 3600


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError(f"connect to https://api.telegram.org/bot{TOKEN}/sendMessage failed"),
        httpx.ReadTimeout(f"timeout https://api.telegram.org/bot{TOKEN}/sendMessage"),
        httpx.Response(400, json={"ok": False, "error_code": 400, "description": f"bad {TOKEN}"}),
        httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}),
        httpx.Response(500, text=f"<html>oops {TOKEN}</html>"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"ok": False}),
    ],
)
async def test_client_errors_do_not_contain_token(
    fake: FakeTelegram, failure: httpx.Response | Exception, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake.responses = [failure]
    api = _api(fake)
    with pytest.raises(BotApiError) as exc:
        await api.send_message(1, "Проверка")
    await api.aclose()
    err = exc.value
    for rendered in (str(err), repr(err), repr(err.args)):
        assert TOKEN not in rendered
        assert "123456:" not in rendered
    # исходное исключение httpx (с URL и токеном) не прицеплено к цепочке
    assert err.__cause__ is None
    assert err.__suppress_context__
    assert TOKEN not in caplog.text


async def test_httpx_request_log_does_not_contain_token(
    fake: FakeTelegram, caplog: pytest.LogCaptureFixture
) -> None:
    """httpx пишет URL запроса в журнал на INFO; токен в URL должен быть скрыт."""
    caplog.set_level(logging.DEBUG)
    api = _api(fake)
    await api.send_message(1, "Проверка")
    await api.aclose()
    assert TOKEN in fake.raw_urls[0]  # токен действительно в URL — и именно его прячем
    assert "HTTP Request" in caplog.text
    assert TOKEN not in caplog.text
    assert "123456:" not in caplog.text


async def test_client_error_keeps_description_separately(fake: FakeTelegram) -> None:
    """Ревью 1.4 m6: описание Telegram — отдельным полем (без токена), не в str()."""
    fake.responses = [
        httpx.Response(
            400,
            json={
                "ok": False,
                "error_code": 400,
                "description": f"Bad Request: bad webhook: IP address is reserved {TOKEN}",
            },
        )
    ]
    api = _api(fake)
    with pytest.raises(BotApiError) as exc:
        await api.call("setWebhook", {"url": "https://x.example.test/"})
    await api.aclose()
    err = exc.value
    assert err.description is not None
    assert "IP address is reserved" in err.description
    assert TOKEN not in err.description
    assert "IP address" not in str(err)
    from app.bot.setup import describe_error

    shown = describe_error(err)
    assert "setWebhook" in shown and "IP address is reserved" in shown
    assert TOKEN not in shown


def test_client_repr_hides_token() -> None:
    api = BotApi(TOKEN)
    assert TOKEN not in repr(api)
    assert TOKEN not in str(vars(api))


# --- Уведомления (каркас для 3.5) -------------------------------------------------------------


def test_notify_cannot_take_message_text() -> None:
    """ФТ-18: в уведомление нельзя передать текст сообщения — только вид уведомления."""
    params = set(inspect.signature(notify).parameters)
    assert params == {"api", "chat_id", "kind", "webapp_url"}


async def test_notification_has_fixed_text_and_button(fake: FakeTelegram) -> None:
    api = _api(fake)
    for kind in NotificationKind:
        await notify(api, chat_id=5, kind=kind, webapp_url=WEBAPP_URL)
    await api.aclose()
    sent = fake.sent()
    assert len(sent) == len(NotificationKind)
    for s in sent:
        assert s["chat_id"] == 5
        _assert_webapp_button(s)
        assert len(s["text"]) < 200


# --- Настройка бота (меню, команды, вебхук) — запускает оператор -----------------------------


async def test_configure_bot_sets_menu_button_commands_and_webhook(
    fake: FakeTelegram, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    api = _api(fake)
    await configure_bot(
        api,
        webapp_url=WEBAPP_URL,
        webhook_url="https://api.example.test/api/v1/telegram/webhook",
        webhook_secret=WEBHOOK_SECRET,
    )
    await api.aclose()
    calls = dict(fake.calls)
    menu = calls["setChatMenuButton"]["menu_button"]
    assert menu["type"] == "web_app"
    assert menu["web_app"] == {"url": WEBAPP_URL}
    commands = [c["command"] for c in calls["setMyCommands"]["commands"]]
    assert "start" in commands
    hook = calls["setWebhook"]
    assert hook["url"] == "https://api.example.test/api/v1/telegram/webhook"
    assert hook["secret_token"] == WEBHOOK_SECRET
    assert hook["allowed_updates"] == ["message"]
    assert WEBHOOK_SECRET not in caplog.text
    assert TOKEN not in caplog.text


async def test_configure_bot_without_webhook_url_skips_set_webhook(fake: FakeTelegram) -> None:
    api = _api(fake)
    await configure_bot(api, webapp_url=WEBAPP_URL, webhook_url=None, webhook_secret=None)
    await api.aclose()
    assert "setWebhook" not in dict(fake.calls)


async def test_configure_bot_requires_https_webhook(fake: FakeTelegram) -> None:
    api = _api(fake)
    with pytest.raises(ValueError, match="https"):
        await configure_bot(
            api,
            webapp_url=WEBAPP_URL,
            webhook_url="http://api.example.test/hook",
            webhook_secret=WEBHOOK_SECRET,
        )
    await api.aclose()
    assert fake.calls == []


# --- Контракт ---------------------------------------------------------------------------------


def test_webhook_route_is_service_only() -> None:
    """Вебхук — служебный маршрут: не в контракте клиента и не в OpenAPI приложения."""
    from tests.test_contract import SERVICE_ROUTES

    assert ("POST", URL) in SERVICE_ROUTES
    assert URL not in app.openapi()["paths"]


# --- Режим разработки: опрос getUpdates, setup --no-webhook, check (без сети) -----------------

HANG = "hang"  # getUpdates «висит» (долгий опрос без новых обновлений)


def _dev_settings(**overrides: Any) -> Settings:
    """Только то, что нужно опросу: без секрета вебхука, секрета сессии и с недоступной БД."""
    base: dict[str, Any] = {
        "telegram_bot_token": SecretStr(TOKEN),
        "webapp_url": WEBAPP_URL,
        "bot_username": "FeelingsTestBot",
        "database_url": "postgresql+asyncpg://nobody:nothing@db.invalid:1/none",
    }
    base.update(overrides)
    return Settings(**base)


class PollTelegram:
    """Подменённый Bot API для опроса: getUpdates отдаёт пакеты по очереди; когда пакеты
    кончились — ставит остановку и отвечает пустым списком (как истёкший долгий опрос)."""

    def __init__(self, stop: asyncio.Event) -> None:
        self.stop = stop
        self.batches: list[Any] = []
        self.send_responses: list[httpx.Response | Exception] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.read_timeouts: list[float | None] = []
        self.raw: list[str] = []
        self.hanging = asyncio.Event()
        self.notify_after_polls: int | None = None
        self.polls_reached = asyncio.Event()

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.raw.append(str(request.url) + request.content.decode())
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content or b"{}")
        self.calls.append((method, payload))
        if method == "getUpdates":
            self.read_timeouts.append(request.extensions["timeout"]["read"])
            if self.notify_after_polls is not None and len(self.polls()) >= self.notify_after_polls:
                self.polls_reached.set()
            if payload.get("timeout") == 0:  # подтверждение при остановке
                return httpx.Response(200, json={"ok": True, "result": []})
            if not self.batches:
                self.stop.set()
                return httpx.Response(200, json={"ok": True, "result": []})
            nxt = self.batches.pop(0)
            if nxt == HANG:
                self.hanging.set()
                await asyncio.Event().wait()
            if isinstance(nxt, Exception):
                raise nxt
            if isinstance(nxt, httpx.Response):
                return nxt
            return httpx.Response(200, json={"ok": True, "result": nxt})
        if method == "sendMessage" and self.send_responses:
            nxt = self.send_responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        result: Any = True if method != "sendMessage" else {"message_id": 1}
        return httpx.Response(200, json={"ok": True, "result": result})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def polls(self) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == "getUpdates"]

    def sent(self) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == "sendMessage"]


async def _run_poll(tg: PollTelegram, stop: asyncio.Event, **kw: Any) -> int | None:
    settings = kw.pop("settings", None) or _dev_settings()
    return await asyncio.wait_for(
        poll_module.run(
            settings, stop=stop, transport=tg.transport(), retry_delays=(0.0, 0.01), **kw
        ),
        5.0,
    )


def _assert_clean(text: str) -> None:
    assert TOKEN not in text
    assert TOKEN.split(":", 1)[1] not in text
    assert PAYLOAD not in text
    assert "ЛИЧНОЕ_ПРИЗНАНИЕ_Qw7" not in text


async def test_poll_deletes_webhook_processes_updates_and_moves_offset(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    stop = asyncio.Event()
    tg = PollTelegram(stop)
    u1 = _message(f"/start {PAYLOAD}")
    u2 = _message("ЛИЧНОЕ_ПРИЗНАНИЕ_Qw7")
    tg.batches = [[u1, u2]]
    offset = await _run_poll(tg, stop)
    assert tg.methods()[0] == "deleteWebhook"
    assert tg.calls[0][1] == {"drop_pending_updates": False}
    first, second = tg.polls()
    assert "offset" not in first
    assert first["timeout"] == 25
    assert first["allowed_updates"] == bot_setup.ALLOWED_UPDATES == ["message"]
    # offset подтверждает только обработанные обновления
    assert second["offset"] == u2["update_id"] + 1 == offset
    # чтение ответа ждёт дольше долгого опроса
    assert all(t is not None and t > 25 for t in tg.read_timeouts)
    sent = tg.sent()
    assert [s["text"] for s in sent] == [START_TEXT, HINT_TEXT]
    for s in sent:
        _assert_webapp_button(s)
    assert "режим разработки" in caplog.text
    assert "в продуктиве — вебхук" in caplog.text
    _assert_clean(caplog.text)


async def test_poll_drop_pending_flag() -> None:
    stop = asyncio.Event()
    tg = PollTelegram(stop)
    await _run_poll(tg, stop, drop_pending=True)
    assert tg.calls[0] == ("deleteWebhook", {"drop_pending_updates": True})


def test_poll_main_parses_drop_pending(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[bool] = []

    async def fake_amain(drop_pending: bool) -> None:
        seen.append(drop_pending)

    monkeypatch.setattr(poll_module, "_amain", fake_amain)
    poll_module.main([])
    poll_module.main(["--drop-pending"])
    assert seen == [False, True]
    err = capsys.readouterr().err
    assert "режим разработки" in err and "в продуктиве — вебхук" in err


async def test_poll_repeated_update_id_replies_once() -> None:
    stop = asyncio.Event()
    tg = PollTelegram(stop)
    u = _message("/start")
    tg.batches = [[u], [u]]  # Telegram прислал то же обновление ещё раз
    await _run_poll(tg, stop)
    assert len(tg.sent()) == 1


async def test_poll_transient_failure_does_not_move_offset_and_retries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    stop = asyncio.Event()
    tg = PollTelegram(stop)
    u1, u2 = _message("/start"), _message(f"/start {PAYLOAD}")
    tg.batches = [[u1, u2], [u2]]
    tg.send_responses = [
        httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}),
        httpx.Response(502, json={"ok": False, "error_code": 502, "description": "Bad Gateway"}),
    ]
    offset = await _run_poll(tg, stop)
    polls = tg.polls()
    # после сбоя на u2 offset сдвинут только за u1 — u2 запрошено снова
    assert polls[1]["offset"] == u1["update_id"] + 1
    assert polls[2]["offset"] == u2["update_id"] + 1 == offset
    assert len(tg.sent()) == 3  # u1, u2 (сбой), u2 (повтор)
    assert "fetched again" in caplog.text
    _assert_clean(caplog.text)


async def test_poll_permanent_bot_api_error_moves_offset() -> None:
    stop = asyncio.Event()
    tg = PollTelegram(stop)
    u = _message("/start")
    tg.batches = [[u]]
    tg.send_responses = [
        httpx.Response(403, json={"ok": False, "error_code": 403, "description": "blocked"})
    ]
    offset = await _run_poll(tg, stop)
    assert offset == u["update_id"] + 1
    assert len(tg.sent()) == 1


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError(f"connect to https://api.telegram.org/bot{TOKEN}/getUpdates"),
        httpx.ReadTimeout(f"read https://api.telegram.org/bot{TOKEN}/getUpdates"),
        httpx.Response(502, text="bad gateway"),
        httpx.Response(409, json={"ok": False, "error_code": 409, "description": "Conflict"}),
        httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {}}),
    ],
)
async def test_poll_retries_get_updates_after_network_errors(
    failure: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    stop = asyncio.Event()
    tg = PollTelegram(stop)
    u = _message("/start")
    tg.batches = [failure, failure, [u]]
    offset = await _run_poll(tg, stop)
    assert offset == u["update_id"] + 1
    assert len(tg.sent()) == 1
    assert len(tg.polls()) == 4
    assert "retry in" in caplog.text
    _assert_clean(caplog.text)


async def test_poll_retry_pause_grows_and_is_interrupted_by_stop() -> None:
    stop = asyncio.Event()
    tg = PollTelegram(stop)
    tg.batches = [httpx.ConnectError("x")] * 3
    loop = asyncio.get_running_loop()
    started = loop.time()
    task = asyncio.create_task(
        poll_module.run(
            _dev_settings(), stop=stop, transport=tg.transport(), retry_delays=(0.0, 0.0, 60.0)
        )
    )
    tg.notify_after_polls = 3
    await asyncio.wait_for(tg.polls_reached.wait(), 5.0)
    await asyncio.sleep(0.05)
    assert not task.done()  # ждёт 60 с перед четвёртым запросом
    stop.set()
    await asyncio.wait_for(task, 2.0)
    assert loop.time() - started < 5
    assert len(tg.polls()) == 3


async def test_poll_bad_token_stops_with_error_without_token() -> None:
    stop = asyncio.Event()
    tg = PollTelegram(stop)
    tg.batches = [httpx.Response(401, json={"ok": False, "error_code": 401})]
    with pytest.raises(poll_module.PollStopped) as info:
        await _run_poll(tg, stop)
    assert info.value.error.error_code == 401
    assert TOKEN not in str(info.value)
    assert len(tg.polls()) == 1


def test_poll_main_exits_without_token_in_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_api(token: Any, **kw: Any) -> BotApi:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
            )

        return BotApi(token, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(poll_module, "BotApi", fake_api)
    monkeypatch.setattr(poll_module, "get_settings", _dev_settings)
    with pytest.raises(SystemExit) as info:
        poll_module.main([])
    msg = str(info.value.code)
    assert "deleteWebhook" in msg and "401" in msg and "Unauthorized" in msg
    out = capsys.readouterr()
    for text in (msg, out.out, out.err):
        assert TOKEN not in text


async def test_poll_stop_during_long_poll_confirms_processed_updates() -> None:
    stop = asyncio.Event()
    tg = PollTelegram(stop)
    u = _message("/start")
    tg.batches = [[u], HANG]
    task = asyncio.create_task(_run_poll(tg, stop))
    await asyncio.wait_for(tg.hanging.wait(), 5.0)
    stop.set()  # Ctrl+C / SIGTERM
    offset = await asyncio.wait_for(task, 2.0)
    assert offset == u["update_id"] + 1
    confirm = tg.polls()[-1]
    assert confirm["offset"] == offset and confirm["timeout"] == 0
    assert len(tg.sent()) == 1


async def test_poll_stop_mid_batch_finishes_current_update_only() -> None:
    stop = asyncio.Event()
    tg = PollTelegram(stop)
    u1, u2 = _message("/start"), _message("/start")
    tg.batches = [[u1, u2]]
    handled: list[int] = []

    async def handler(api: BotApi, update: dict[str, Any], webapp_url: str) -> str:
        handled.append(update["update_id"])
        stop.set()  # сигнал пришёл во время обработки первого
        await asyncio.sleep(0)
        return "start"

    api = BotApi(TOKEN, transport=tg.transport())
    offset = await asyncio.wait_for(
        poll_module.poll_updates(api, webapp_url=WEBAPP_URL, stop=stop, handler=handler), 5.0
    )
    await api.aclose()
    assert handled == [u1["update_id"]]
    assert offset == u1["update_id"] + 1
    assert tg.polls()[-1] == {
        "offset": offset,
        "timeout": 0,
        "limit": 1,
        "allowed_updates": ["message"],
    }


async def test_poll_processing_deadline_retries_update() -> None:
    stop = asyncio.Event()
    tg = PollTelegram(stop)
    u = _message("/start")
    tg.batches = [[u], [u]]
    calls: list[int] = []

    async def slow_once(api: BotApi, update: dict[str, Any], webapp_url: str) -> str:
        calls.append(update["update_id"])
        if len(calls) == 1:
            await asyncio.Event().wait()
        return "start"

    api = BotApi(TOKEN, transport=tg.transport())
    offset = await asyncio.wait_for(
        poll_module.poll_updates(
            api,
            webapp_url=WEBAPP_URL,
            stop=stop,
            handler=slow_once,
            deadline_s=0.05,
            retry_delays=(0.0,),
        ),
        5.0,
    )
    await api.aclose()
    assert calls == [u["update_id"], u["update_id"]]
    assert tg.polls()[1].get("offset") is None  # после срока — то же обновление снова
    assert offset == u["update_id"] + 1


async def test_poll_sigterm_stops_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    holder: dict[str, PollTelegram] = {}

    def fake_api(token: Any, **kw: Any) -> BotApi:
        return BotApi(token, transport=holder["tg"].transport())

    monkeypatch.setattr(poll_module, "BotApi", fake_api)
    monkeypatch.setattr(poll_module, "get_settings", _dev_settings)
    tg = PollTelegram(asyncio.Event())
    tg.batches = [HANG]
    holder["tg"] = tg
    task = asyncio.create_task(poll_module._amain(False))
    await asyncio.wait_for(tg.hanging.wait(), 5.0)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 2.0)
    assert tg.methods() == ["deleteWebhook", "getUpdates"]
    # обработчики сигналов сняты
    assert not asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)


def test_poll_needs_no_webhook_secret_or_database() -> None:
    settings = _dev_settings()
    assert settings.telegram_webhook_secret.get_secret_value() == ""
    assert settings.session_secret.get_secret_value() == ""
    check_dev_bot_config(settings)
    poll_module.check_poll_config(settings)
    # проверка сервера не ослаблена: без секрета вебхука сервис не стартует
    with pytest.raises(ConfigError, match="WEBHOOK_SECRET"):
        check_bot_config(settings)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("telegram_bot_token", SecretStr("")),
        ("webapp_url", ""),
        ("webapp_url", "http://app.example.test/"),
        ("webapp_url", "https://"),
    ],
)
def test_dev_bot_config_requires_token_and_https_webapp(field: str, value: Any) -> None:
    with pytest.raises(ConfigError) as exc:
        check_dev_bot_config(_dev_settings(**{field: value}))
    assert TOKEN not in str(exc.value)


@pytest.mark.parametrize(
    "env", ["prod", "production", " PROD ", "prd", "live", "staging", "", "development"]
)
async def test_poll_refuses_production_env(env: str) -> None:
    """A8 D-5: разрешённый список окружений — всё, кроме dev/test/local, отклоняется."""
    stop = asyncio.Event()
    tg = PollTelegram(stop)
    with pytest.raises(ConfigError, match="development only"):
        await _run_poll(tg, stop, settings=_dev_settings(env=env))
    assert tg.calls == []  # вебхук продуктива не тронут


# --- setup --no-webhook -------------------------------------------------------------------------


def _patch_api(monkeypatch: pytest.MonkeyPatch, module: Any, fake: FakeTelegram) -> None:
    def factory(token: Any, **kw: Any) -> BotApi:
        return BotApi(token, transport=httpx.MockTransport(fake.handler))

    monkeypatch.setattr(module, "BotApi", factory)


def test_setup_no_webhook_sets_only_commands_and_menu_button(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake: FakeTelegram,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    _patch_api(monkeypatch, bot_setup, fake)
    monkeypatch.setattr(bot_setup, "get_settings", _dev_settings)  # секрета вебхука нет
    bot_setup.main(["--no-webhook"])
    assert sorted(m for m, _ in fake.calls) == ["setChatMenuButton", "setMyCommands"]
    menu = dict(fake.calls)["setChatMenuButton"]["menu_button"]
    assert menu == {"type": "web_app", "text": "Открыть", "web_app": {"url": WEBAPP_URL}}
    out = capsys.readouterr()
    assert "вебхук не менялся" in out.out
    for text in (out.out, out.err, caplog.text):
        assert TOKEN not in text


def test_setup_without_flag_still_requires_webhook_secret(
    monkeypatch: pytest.MonkeyPatch, fake: FakeTelegram
) -> None:
    _patch_api(monkeypatch, bot_setup, fake)
    monkeypatch.setattr(bot_setup, "get_settings", _dev_settings)
    with pytest.raises(SystemExit) as info:
        bot_setup.main([])
    assert "WEBHOOK_SECRET" in str(info.value.code)
    assert fake.calls == []


def test_setup_no_webhook_conflicts_with_webhook_url() -> None:
    with pytest.raises(SystemExit):
        bot_setup.main(["--no-webhook", "--webhook-url", "https://api.example.test/hook"])


# --- check (getMe) ------------------------------------------------------------------------------


def _check_fake(me: httpx.Response, hook_url: str = "") -> FakeTelegram:
    fake = FakeTelegram()
    info = {"url": hook_url, "pending_update_count": 2}
    fake.responses = [me, httpx.Response(200, json={"ok": True, "result": info})]
    return fake


def _me(username: str = "FeelingsTestBot") -> httpx.Response:
    bot_id = int(TOKEN.split(":", 1)[0])
    result = {"id": bot_id, "is_bot": True, "first_name": "Чувства", "username": username}
    return httpx.Response(200, json={"ok": True, "result": result})


async def test_check_reports_username_and_working_token() -> None:
    fake = _check_fake(_me())
    lines = await check_module.check_bot(
        _dev_settings(webapp_url=""), transport=httpx.MockTransport(fake.handler)
    )
    text = "\n".join(lines)
    assert "Токен рабочий: бот @FeelingsTestBot" in text
    assert "https://t.me/FeelingsTestBot" in text
    assert "Вебхук: не задан" in text
    assert [m for m, _ in fake.calls] == ["getMe", "getWebhookInfo"]
    assert TOKEN not in text
    assert TOKEN.split(":", 1)[0] not in text  # id бота — часть токена


async def test_check_warns_on_username_mismatch_and_webhook_set() -> None:
    fake = _check_fake(_me("OtherTestBot"), hook_url="https://api.example.test/hook")
    lines = await check_module.check_bot(
        _dev_settings(), transport=httpx.MockTransport(fake.handler)
    )
    text = "\n".join(lines)
    assert "не совпадает с @OtherTestBot" in text
    assert "Вебхук: задан" in text
    assert "api.example.test" not in text


def test_check_main_prints_result_without_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = _check_fake(_me())
    _patch_api(monkeypatch, check_module, fake)
    monkeypatch.setattr(check_module, "get_settings", lambda: _dev_settings(webapp_url=""))
    check_module.main()
    out = capsys.readouterr()
    assert "Токен рабочий: бот @FeelingsTestBot" in out.out
    for text in (out.out, out.err, caplog.text):
        assert TOKEN not in text
        assert TOKEN.split(":", 1)[1] not in text


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            httpx.Response(
                401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
            ),
            "Токен не работает",
        ),
        (httpx.ConnectError(f"https://api.telegram.org/bot{TOKEN}/getMe"), "Не удалось"),
    ],
)
def test_check_main_fails_without_token_in_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    response: Any,
    expected: str,
) -> None:
    fake = FakeTelegram()
    fake.responses = [response]
    _patch_api(monkeypatch, check_module, fake)
    monkeypatch.setattr(check_module, "get_settings", _dev_settings)
    with pytest.raises(SystemExit) as info:
        check_module.main()
    msg = str(info.value.code)
    assert msg.startswith(expected)
    out = capsys.readouterr()
    for text in (msg, out.out, out.err):
        assert TOKEN not in text


def test_check_main_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        check_module, "get_settings", lambda: _dev_settings(telegram_bot_token=SecretStr(""))
    )
    with pytest.raises(SystemExit) as info:
        check_module.main()
    assert "FEELINGS_TELEGRAM_BOT_TOKEN" in str(info.value.code)


@pytest.mark.parametrize("env", ["dev", "test", "local", "DEV", " Local "])
def test_poll_allows_only_dev_envs(env: str) -> None:
    poll_module.check_poll_config(_dev_settings(env=env))


def test_poll_default_env_is_dev() -> None:
    """По умолчанию Settings.env = "dev" — опрос разрешён (осознанно, для разработчика);
    на продуктиве FEELINGS_ENV обязателен (bot/README.md)."""
    assert Settings.model_fields["env"].default == "dev"
    poll_module.check_poll_config(_dev_settings())


@pytest.mark.parametrize("result", [{"not": "a list"}, None, "junk", 7])
async def test_poll_non_list_result_is_transient_with_pause(
    result: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A8 D-4: ok:true с результатом не списком — временный сбой: пауза перед повтором."""
    caplog.set_level(logging.DEBUG)
    stop = asyncio.Event()
    tg = PollTelegram(stop)
    u = _message("/start")
    bad = httpx.Response(200, json={"ok": True, "result": result})
    tg.batches = [bad, bad, [u]]
    pauses: list[float] = []
    real_pause = poll_module._pause

    async def spy(stop_event: asyncio.Event, delay: float) -> None:
        pauses.append(delay)
        await real_pause(stop_event, 0.0)

    monkeypatch.setattr(poll_module, "_pause", spy)
    offset = await _run_poll(tg, stop)
    assert pauses == [0.0, 0.01]  # нарастающая пауза (retry_delays теста)
    assert offset == u["update_id"] + 1
    assert len(tg.sent()) == 1
    assert "unexpected result; retry in" in caplog.text

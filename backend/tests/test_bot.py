"""1.4 / ФТ-03, ФТ-18: Telegram-бот — вебхук, /start с кнопкой Mini App, идемпотентность по
update_id, клиент Bot API (таймауты, 429, токен вне журналов и ошибок). Сети нет: Bot API
подменён httpx.MockTransport. Данные — выдуманные."""

import inspect
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.bot import webhook as webhook_module
from app.bot.client import BotApi, BotApiError
from app.bot.dedup import UpdateDeduplicator
from app.bot.notifications import NotificationKind, notify
from app.bot.setup import configure_bot
from app.config import ConfigError, Settings, check_bot_config, get_settings
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
    assert resp.status_code in (401, 403)
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

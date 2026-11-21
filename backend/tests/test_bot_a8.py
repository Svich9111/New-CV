"""A8, задача 1.4 — независимые тесты Telegram-бота (ФТ-03, ФТ-18; ТЗ §4.1 п.1, 3, 4; §4.4 п.3;
§9.1 п.5). Без сети: Bot API подменён httpx.MockTransport, вебхук вызывается через
httpx.ASGITransport. Хелперы исполнителя (tests/test_bot.py) не используются. Данные выдуманные."""

import asyncio
import inspect
import json
import logging
import traceback
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.bot import webhook as webhook_module
from app.bot.client import BotApi, BotApiError
from app.bot.dedup import UpdateDeduplicator
from app.bot.notifications import NotificationKind, notify
from app.config import ConfigError, Settings, check_bot_config, get_settings
from app.main import app

# Похож на настоящий токен: «<id бота>:<35 символов base64url>».
BOT_TOKEN = "7012345678:AAHq3xY_fake-Token-for-A8-tests-0001"
BOT_ID = BOT_TOKEN.split(":", 1)[0]
TOKEN_TAIL = BOT_TOKEN.split(":", 1)[1]
HOOK_SECRET = "A8_hook-Secret_" + "q" * 33
WEBAPP = "https://mini.feelings.example/app"
PATH = "/api/v1/telegram/webhook"
HEADER = "X-Telegram-Bot-Api-Secret-Token"
# payload /start: до 64 символов A-Z a-z 0-9 _ - (правила Telegram для deep link)
PAYLOAD_64 = "Inv1te_" + "Zq9-" * 14 + "x"
USER_TEXT = "Личное: Игорь опять забыл про ужин, я злюсь"
assert len(PAYLOAD_64) == 64


def a8_settings() -> Settings:
    return Settings(
        telegram_bot_token=SecretStr(BOT_TOKEN),
        session_secret=SecretStr("z" * 48),
        bot_username="FeelingsA8Bot",
        telegram_webhook_secret=SecretStr(HOOK_SECRET),
        webapp_url=WEBAPP,
    )


class TgStub:
    """Подмена api.telegram.org: считает реальные вызовы и отвечает по сценарию."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.script: list[httpx.Response | Exception] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.gate is not None:
            await self.gate.wait()
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    def methods(self) -> list[str]:
        return [r.url.path.rsplit("/", 1)[-1] for r in self.requests]

    def bodies(self, method: str = "sendMessage") -> list[dict[str, Any]]:
        return [
            json.loads(r.content) for r in self.requests if r.url.path.rsplit("/", 1)[-1] == method
        ]


class Rig:
    def __init__(self, stub: TgStub, http: httpx.AsyncClient, sleeps: list[float]) -> None:
        self.stub, self.http, self.sleeps = stub, http, sleeps

    async def post(self, body: Any, secret: str | None = HOOK_SECRET, raw: bytes | None = None):
        headers = {} if secret is None else {HEADER: secret}
        content = raw if raw is not None else json.dumps(body, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
        return await self.http.post(PATH, content=content, headers=headers)


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    stub = TgStub()
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    saved = app.state.bot_api, app.state.bot_updates
    api = BotApi(BOT_TOKEN, transport=httpx.MockTransport(stub), sleep=fake_sleep)
    app.state.bot_api = api
    app.state.bot_updates = UpdateDeduplicator()
    app.dependency_overrides[get_settings] = a8_settings
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://a8"
        ) as http:
            yield Rig(stub, http, sleeps)
    finally:
        app.dependency_overrides.pop(get_settings, None)
        await api.aclose()
        app.state.bot_api, app.state.bot_updates = saved


_seq = [910_000_000]


def next_uid() -> int:
    _seq[0] += 1
    return _seq[0]


def tg_message(
    text: str | None,
    *,
    chat_type: str = "private",
    entities: bool = True,
    update_id: int | None = None,
    kind: str = "message",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Обновление в формате Bot API (как присылает Telegram)."""
    chat: dict[str, Any] = {"id": 555_000_111, "type": chat_type}
    if chat_type == "private":
        chat |= {"first_name": "Анна", "username": "anna_a8"}
    else:
        chat = {"id": -100_123_456_789, "type": chat_type, "title": "Семья"}
    msg: dict[str, Any] = {"message_id": 314, "date": 1_763_700_000, "chat": chat}
    if chat_type != "channel":
        msg["from"] = {
            "id": 555_000_111,
            "is_bot": False,
            "first_name": "Анна",
            "language_code": "ru",
        }
    if text is not None:
        msg["text"] = text
        if entities and text.startswith("/"):
            cmd_len = len(text.split(maxsplit=1)[0])
            msg["entities"] = [{"offset": 0, "length": cmd_len, "type": "bot_command"}]
    if extra:
        msg |= extra
    return {"update_id": update_id if update_id is not None else next_uid(), kind: msg}


def web_app_urls(body: dict[str, Any]) -> list[str]:
    rows = body["reply_markup"]["inline_keyboard"]
    return [b["web_app"]["url"] for row in rows for b in row if "web_app" in b]


def assert_button_to_webapp(body: dict[str, Any]) -> None:
    urls = web_app_urls(body)
    assert urls == [WEBAPP], body
    assert all(u.startswith("https://") for u in urls)


# ---------------------------------------------------------------------------------------------
# §4.1 п.1, ФТ-03: /start в реальных форматах обновлений
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "/start",
        "/start@FeelingsA8Bot",
        f"/start {PAYLOAD_64}",
        f"/start@FeelingsA8Bot {PAYLOAD_64}",
        "/START",
        "/start\n" + PAYLOAD_64,
    ],
)
async def test_start_real_formats_reply_with_webapp_button(
    rig: Rig, text: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    r = await rig.post(tg_message(text))
    assert r.status_code == 200
    bodies = rig.stub.bodies()
    assert len(bodies) == 1
    assert bodies[0]["chat_id"] == 555_000_111
    assert_button_to_webapp(bodies[0])
    # payload не возвращается пользователю и не попадает в журнал (§9.1 п.5)
    assert PAYLOAD_64 not in json.dumps(bodies[0], ensure_ascii=False)
    assert PAYLOAD_64 not in caplog.text
    assert PAYLOAD_64[:20] not in caplog.text
    assert r.content in (b"", b"null")


async def test_start_without_entities_still_answered(rig: Rig) -> None:
    r = await rig.post(tg_message("/start", entities=False))
    assert r.status_code == 200
    assert len(rig.stub.bodies()) == 1


@pytest.mark.parametrize(
    "kind", ["edited_message", "channel_post", "edited_channel_post", "business_message"]
)
async def test_start_in_other_update_kinds_gets_no_reply(rig: Rig, kind: str) -> None:
    r = await rig.post(tg_message("/start", kind=kind))
    assert r.status_code == 200
    assert rig.stub.requests == []


@pytest.mark.parametrize(
    "update",
    [
        {
            "callback_query": {
                "id": "4382bfdwdsb323b2d9",
                "from": {"id": 555_000_111, "is_bot": False, "first_name": "Анна"},
                "chat_instance": "-123",
                "data": "/start " + PAYLOAD_64,
            }
        },
        {
            "my_chat_member": {
                "chat": {"id": 555_000_111, "type": "private", "first_name": "Анна"},
                "from": {"id": 555_000_111, "is_bot": False, "first_name": "Анна"},
                "date": 1_763_700_000,
                "old_chat_member": {"status": "member", "user": {"id": 1, "is_bot": True}},
                "new_chat_member": {"status": "kicked", "user": {"id": 1, "is_bot": True}},
            }
        },
        {"message_reaction": {"chat": {"id": 1, "type": "private"}}},
        {"inline_query": {"id": "1", "query": USER_TEXT, "offset": ""}},
    ],
    ids=["callback_query", "my_chat_member", "message_reaction", "inline_query"],
)
async def test_non_message_updates_are_acknowledged_silently(
    rig: Rig, update: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    r = await rig.post({"update_id": next_uid(), **update})
    assert r.status_code == 200
    assert rig.stub.requests == []
    assert USER_TEXT not in caplog.text
    assert PAYLOAD_64 not in caplog.text


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
@pytest.mark.parametrize("text", ["/start", "/start@FeelingsA8Bot " + PAYLOAD_64, USER_TEXT])
async def test_groups_and_channels_get_no_reply(rig: Rig, chat_type: str, text: str) -> None:
    r = await rig.post(tg_message(text, chat_type=chat_type))
    assert r.status_code == 200
    assert rig.stub.requests == []


@pytest.mark.parametrize(
    "extra",
    [
        {"sticker": {"file_id": "CAAC", "file_unique_id": "u", "width": 512, "height": 512}},
        {
            "photo": [{"file_id": "p", "file_unique_id": "u", "width": 1, "height": 1}],
            "caption": "/start " + PAYLOAD_64,
        },
        {"voice": {"file_id": "v", "file_unique_id": "u", "duration": 3}},
    ],
    ids=["sticker", "photo_with_caption", "voice"],
)
async def test_messages_without_text_are_ignored(rig: Rig, extra: dict[str, Any]) -> None:
    r = await rig.post(tg_message(None, extra=extra))
    assert r.status_code == 200
    assert rig.stub.requests == []


async def test_plain_text_gets_hint_without_echo(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    r = await rig.post(tg_message(USER_TEXT))
    assert r.status_code == 200
    (body,) = rig.stub.bodies()
    assert USER_TEXT not in body["text"]
    assert "Игорь" not in body["text"]
    assert_button_to_webapp(body)
    assert USER_TEXT not in caplog.text
    assert "Игорь" not in caplog.text


# ---------------------------------------------------------------------------------------------
# Неразборчивые тела запроса (критерий «не роняют обработчик»)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json at all",
        b"{",
        b"\xff\xfe\x00garbage",
        b"[]",
        b"null",
        b"42",
        b'"update"',
        b'{"update_id": "12"}',
        b'{"update_id": true, "message": {}}',
        b'{"update_id": 1.5}',
        b'{"message": {"text": "/start"}}',
        b'{"update_id": 5, "message": null}',
        b'{"update_id": 6, "message": {"chat": "x", "text": "/start"}}',
        b'{"update_id": 7, "message": {"chat": {"id": "1", "type": "private"}, "text": "/s"}}',
        b'{"update_id": 8, "message": {"chat": {"id": 1, "type": "private"}, "text": 5}}',
    ],
)
async def test_malformed_bodies_are_200_without_reply(rig: Rig, raw: bytes) -> None:
    r = await rig.post(None, raw=raw)
    assert r.status_code == 200
    assert rig.stub.requests == []


async def test_huge_body_does_not_crash(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    big = "Ж" * 2_000_000
    r = await rig.post(tg_message(big))
    assert r.status_code == 200
    assert len(rig.stub.bodies()) == 1
    assert big[:100] not in caplog.text
    assert big[:100] not in rig.stub.requests[0].content.decode()


async def test_deeply_nested_json_is_ignored(rig: Rig) -> None:
    # D-1 (раунд 1) исправлен: RecursionError разбора — 200 «ignored», без ответа и без повтора
    for raw in (b"[" * 200_000, b'{"a":' * 100_000):
        r = await rig.post(None, raw=raw)
        assert r.status_code == 200
    assert rig.stub.requests == []


# ---------------------------------------------------------------------------------------------
# Секретный заголовок (§9.1, критерий 1)
# ---------------------------------------------------------------------------------------------


async def test_missing_secret_is_401_and_no_reply(rig: Rig) -> None:
    r = await rig.post(tg_message("/start"), secret=None)
    assert r.status_code == 401
    assert rig.stub.requests == []


@pytest.mark.parametrize(
    "secret",
    [
        "",
        HOOK_SECRET.upper(),
        HOOK_SECRET.lower(),
        HOOK_SECRET.swapcase(),
        " " + HOOK_SECRET,
        HOOK_SECRET + " ",
        HOOK_SECRET[:-1],
        HOOK_SECRET + "q",
        HOOK_SECRET * 50,
        "x" * 8192,
        BOT_TOKEN,
    ],
    ids=[
        "empty",
        "upper",
        "lower",
        "swapcase",
        "lead_space",
        "trail_space",
        "prefix",
        "longer",
        "repeated",
        "8k",
        "bot_token",
    ],
)
async def test_wrong_secret_is_403_and_no_reply(rig: Rig, secret: str) -> None:
    r = await rig.post(tg_message("/start"), secret=secret)
    assert r.status_code == 403
    assert rig.stub.requests == []
    assert HOOK_SECRET not in r.text
    assert secret not in r.text or secret == ""


async def test_secret_header_name_case_insensitive(rig: Rig) -> None:
    r = await rig.http.post(
        PATH,
        content=json.dumps(tg_message("/start")).encode(),
        headers={HEADER.lower(): HOOK_SECRET},
    )
    assert r.status_code == 200
    assert len(rig.stub.bodies()) == 1


async def test_non_ascii_secret_header_is_403_not_500(rig: Rig) -> None:
    r = await rig.http.post(
        PATH,
        content=b"{}",
        headers=[(HEADER.encode(), "секрет".encode())],
    )
    assert r.status_code == 403


async def test_wrong_secret_rejected_before_body_is_read(rig: Rig) -> None:
    r = await rig.post(None, raw=b"[" * 200_000, secret="wrong")
    assert r.status_code == 403


async def test_secret_compared_with_compare_digest(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hmac

    seen: list[tuple[Any, Any]] = []
    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(webhook_module.hmac, "compare_digest", spy)
    await rig.post(tg_message("/start"), secret="nope")
    await rig.post(tg_message("/start"))
    assert len(seen) == 2


async def test_unconfigured_secret_is_503_for_everyone(rig: Rig) -> None:
    def blank() -> Settings:
        s = a8_settings()
        s.telegram_webhook_secret = SecretStr("")
        return s

    app.dependency_overrides[get_settings] = blank
    codes = [
        (await rig.post(tg_message("/start"), secret=s)).status_code
        for s in ("", None, HOOK_SECRET)
    ]
    assert codes == [503, 503, 503]
    assert rig.stub.requests == []


def test_startup_rejects_blank_or_weak_webhook_secret() -> None:
    for bad in ["", " " * 40, "short", "a" * 31, "a" * 257, "a" * 31 + " ", "кириллица" * 5]:
        s = a8_settings()
        s.telegram_webhook_secret = SecretStr(bad)
        with pytest.raises(ConfigError) as exc:
            check_bot_config(s)
        assert bad.strip() == "" or bad not in str(exc.value)


# ---------------------------------------------------------------------------------------------
# Идемпотентность по update_id (критерий 3)
# ---------------------------------------------------------------------------------------------


async def test_redelivery_of_same_update_id_sends_once(rig: Rig) -> None:
    upd = tg_message("/start " + PAYLOAD_64)
    for _ in range(5):
        r = await rig.post(upd)
        assert r.status_code == 200
    assert len(rig.stub.bodies()) == 1


async def test_same_update_id_with_different_content_is_still_duplicate(rig: Rig) -> None:
    uid = next_uid()
    await rig.post(tg_message("/start", update_id=uid))
    await rig.post(tg_message("/help", update_id=uid))
    assert len(rig.stub.bodies()) == 1


async def test_concurrent_identical_deliveries_send_once(rig: Rig) -> None:
    upd = tg_message("/start")
    rig.stub.gate = asyncio.Event()

    async def release_later() -> None:
        await asyncio.sleep(0.05)
        assert rig.stub.gate is not None
        rig.stub.gate.set()

    r1, r2, _ = await asyncio.gather(rig.post(upd), rig.post(upd), release_later())
    assert (r1.status_code, r2.status_code) == (200, 200)
    assert len(rig.stub.bodies()) == 1


async def test_concurrent_ten_identical_deliveries_send_once(rig: Rig) -> None:
    upd = tg_message("/start")
    results = await asyncio.gather(*(rig.post(upd) for _ in range(10)))
    assert all(r.status_code == 200 for r in results)
    assert len(rig.stub.bodies()) == 1


async def test_duplicate_during_inflight_transient_failure_is_not_lost(rig: Rig) -> None:
    # D-2 (раунд 1) исправлен: повтор ждёт исхода первой доставки
    upd = tg_message("/start")
    rig.stub.gate = asyncio.Event()
    rig.stub.script = [httpx.Response(502, text="Bad Gateway")]

    async def release_later() -> None:
        await asyncio.sleep(0.05)
        assert rig.stub.gate is not None
        rig.stub.gate.set()

    r1, r2, _ = await asyncio.gather(rig.post(upd), rig.post(upd), release_later())
    # ожидается: хотя бы одна из доставок не подтверждена (Telegram повторит)
    assert sorted([r1.status_code, r2.status_code]) != [200, 503]
    # точнее: обе — 503, а следующая доставка обрабатывается (ответ уходит)
    assert (r1.status_code, r2.status_code) == (503, 503)
    assert (await rig.post(upd)).status_code == 200
    assert len(rig.stub.bodies()) == 2


# ---------------------------------------------------------------------------------------------
# Сбои Bot API: что отвечает вебхук и сколько sendMessage ушло (критерии 3 и 6)
# ---------------------------------------------------------------------------------------------


def tg_error(code: int, retry_after: Any = None) -> httpx.Response:
    body: dict[str, Any] = {"ok": False, "error_code": code, "description": f"err {BOT_TOKEN}"}
    if retry_after is not None:
        body["parameters"] = {"retry_after": retry_after}
    return httpx.Response(code, json=body)


async def test_429_short_retry_after_then_ok(rig: Rig) -> None:
    rig.stub.script = [tg_error(429, 3)]
    r = await rig.post(tg_message("/start"))
    assert r.status_code == 200
    assert len(rig.stub.bodies()) == 2
    assert rig.sleeps == [3]


async def test_429_persisting_gives_503_after_bounded_retries(rig: Rig) -> None:
    rig.stub.script = [tg_error(429, 2) for _ in range(10)]
    upd = tg_message("/start")
    r = await rig.post(upd)
    assert r.status_code == 503
    assert len(rig.stub.bodies()) == 3  # 1 + max_retries=2
    assert rig.sleeps == [2, 2]
    # повторная доставка — снова обрабатывается (отметка снята)
    rig.stub.script = []
    r2 = await rig.post(upd)
    assert r2.status_code == 200
    assert len(rig.stub.bodies()) == 4


@pytest.mark.parametrize("retry_after", [11, 60, 3600, 10**9])
async def test_429_too_long_retry_after_no_sleep_503(rig: Rig, retry_after: int) -> None:
    rig.stub.script = [tg_error(429, retry_after)]
    r = await rig.post(tg_message("/start"))
    assert r.status_code == 503
    assert len(rig.stub.bodies()) == 1
    assert rig.sleeps == []


@pytest.mark.parametrize("retry_after", ["5", None, -1, 2.5])
async def test_429_odd_retry_after_is_bounded(rig: Rig, retry_after: Any) -> None:
    rig.stub.script = [tg_error(429, retry_after) for _ in range(10)]
    r = await rig.post(tg_message("/start"))
    assert r.status_code == 503
    assert len(rig.stub.bodies()) <= 3
    assert all(0 <= s <= 10 for s in rig.sleeps) or retry_after == -1


@pytest.mark.parametrize("code", [500, 502, 503, 504])
async def test_5xx_gives_503_and_redelivery_processed_once(rig: Rig, code: int) -> None:
    rig.stub.script = [httpx.Response(code, text="<html>upstream</html>")]
    upd = tg_message("/start")
    assert (await rig.post(upd)).status_code == 503
    assert len(rig.stub.bodies()) == 1  # 5xx не повторяется клиентом
    assert (await rig.post(upd)).status_code == 200
    assert (await rig.post(upd)).status_code == 200
    assert len(rig.stub.bodies()) == 2


@pytest.mark.parametrize("code", [500, 502])
async def test_5xx_json_error_body_is_transient(rig: Rig, code: int) -> None:
    """Bot API отвечает {"ok": false, "error_code": 5xx} — тоже временный сбой."""
    rig.stub.script = [tg_error(code)]
    upd = tg_message("/start")
    assert (await rig.post(upd)).status_code == 503
    assert (await rig.post(upd)).status_code == 200
    assert len(rig.stub.bodies()) == 2


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectTimeout("connect timed out"),
        httpx.ConnectError("refused"),
        httpx.RemoteProtocolError("peer closed"),
    ],
)
async def test_timeout_and_network_errors_give_503_single_send(rig: Rig, exc: Exception) -> None:
    rig.stub.script = [exc]
    upd = tg_message("/start")
    r = await rig.post(upd)
    assert r.status_code == 503
    assert len(rig.stub.bodies()) == 1
    assert rig.sleeps == []
    assert (await rig.post(upd)).status_code == 200
    assert len(rig.stub.bodies()) == 2


@pytest.mark.parametrize("code", [400, 403])
async def test_permanent_errors_are_200_and_not_retried(rig: Rig, code: int) -> None:
    rig.stub.script = [tg_error(code)]
    upd = tg_message("/start")
    assert (await rig.post(upd)).status_code == 200
    assert (await rig.post(upd)).status_code == 200
    assert len(rig.stub.bodies()) == 1


async def test_unexpected_handler_exception_is_200_without_traceback(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    async def boom(*_: Any, **__: Any) -> str:
        raise RuntimeError(f"boom {USER_TEXT} {BOT_TOKEN}")

    monkeypatch.setattr(webhook_module, "handle_update", boom)
    r = await rig.post(tg_message(USER_TEXT))
    assert r.status_code == 200
    assert USER_TEXT not in caplog.text
    assert BOT_TOKEN not in caplog.text
    assert TOKEN_TAIL not in caplog.text


def test_client_timeouts_are_finite() -> None:
    api = BotApi(BOT_TOKEN)
    t = api.timeout
    assert t.connect is not None and 0 < t.connect <= 30
    assert t.read is not None and 0 < t.read <= 30


# ---------------------------------------------------------------------------------------------
# Токен бота нигде не появляется (критерии 5, 6; §9.1)
# ---------------------------------------------------------------------------------------------


def _all_loggers_debug(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    for name in (
        "httpx",
        "httpcore",
        "app",
        "app.bot",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "asyncio",
        "fastapi",
    ):
        caplog.set_level(logging.DEBUG, logger=name)


def _assert_no_token(text: str) -> None:
    assert BOT_TOKEN not in text
    assert TOKEN_TAIL not in text
    assert f"bot{BOT_ID}" not in text


SCENARIOS: list[list[httpx.Response | Exception]] = [
    [],
    [tg_error(429, 1), tg_error(429, 1), tg_error(429, 1)],
    [tg_error(429, 999)],
    [tg_error(403)],
    [tg_error(400)],
    [tg_error(401)],
    [httpx.Response(500, text=f"<html>{BOT_TOKEN}</html>")],
    [httpx.Response(200, text=f"proxy page {BOT_TOKEN}")],
    [httpx.ReadTimeout(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage")],
    [httpx.ConnectError(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage")],
]


@pytest.mark.parametrize("script", SCENARIOS)
async def test_token_absent_from_logs_and_webhook_responses(
    rig: Rig, script: list[Any], caplog: pytest.LogCaptureFixture
) -> None:
    _all_loggers_debug(caplog)
    rig.stub.script = list(script)
    r = await rig.post(tg_message("/start " + PAYLOAD_64))
    assert BOT_TOKEN in str(rig.stub.requests[0].url)  # токен действительно в URL
    for rec in caplog.records:
        _assert_no_token(rec.getMessage())
        if rec.exc_text:
            _assert_no_token(rec.exc_text)
    _assert_no_token(caplog.text)
    _assert_no_token(r.text)
    _assert_no_token(str(r.headers))
    assert PAYLOAD_64 not in caplog.text


@pytest.mark.parametrize("script", SCENARIOS[1:])
async def test_token_absent_from_exception_everywhere(script: list[Any]) -> None:
    stub = TgStub()
    stub.script = list(script)

    async def no_sleep(_: float) -> None:
        return None

    api = BotApi(BOT_TOKEN, transport=httpx.MockTransport(stub), sleep=no_sleep)
    try:
        with pytest.raises(BotApiError) as info:
            await api.send_message(1, USER_TEXT)
    finally:
        await api.aclose()
    err = info.value
    rendered = "".join(traceback.format_exception(err))
    for text in (str(err), repr(err), repr(err.args), rendered, repr(vars(err))):
        _assert_no_token(text)
        assert USER_TEXT not in text
    assert err.__cause__ is None
    assert err.__suppress_context__ is True


def test_token_absent_from_object_reprs() -> None:
    api = BotApi(BOT_TOKEN)
    s = a8_settings()
    for text in (repr(api), str(api), repr(vars(api)), repr(s), str(s), str(s.model_dump())):
        _assert_no_token(text)
        assert HOOK_SECRET not in text


async def test_httpx_info_log_present_but_redacted(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    _all_loggers_debug(caplog)
    await rig.post(tg_message("/start"))
    http_lines = [
        r for r in caplog.records if r.name == "httpx" and "api.telegram.org" in r.getMessage()
    ]
    assert http_lines, "журнал httpx не записан — фильтр проверить нечем"
    for rec in http_lines:
        _assert_no_token(rec.getMessage())
        assert "sendMessage" in rec.getMessage()


async def test_texts_and_payload_absent_from_logs_for_all_outcomes(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    _all_loggers_debug(caplog)
    rig.stub.script = [tg_error(500), tg_error(403)]
    await rig.post(tg_message("/start " + PAYLOAD_64))
    await rig.post(tg_message(USER_TEXT))
    await rig.post(tg_message("/start " + PAYLOAD_64, chat_type="group"))
    dup = tg_message("/start " + PAYLOAD_64)
    await rig.post(dup)
    await rig.post(dup)
    assert caplog.records
    for rec in caplog.records:
        msg = rec.getMessage()
        assert PAYLOAD_64 not in msg
        assert USER_TEXT not in msg
        assert "Игорь" not in msg
        assert "anna_a8" not in msg
        assert "555000111" not in msg  # chat_id/user_id — тоже не нужен журналу


# ---------------------------------------------------------------------------------------------
# Кнопка web_app ведёт на FEELINGS_WEBAPP_URL (https) — §4.1 п.1, п.4
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["http://mini.feelings.example/", "", "https://", "javascript:alert(1)", "ftp://x.y/"]
)
def test_webapp_url_must_be_https(url: str) -> None:
    s = a8_settings()
    s.webapp_url = url
    with pytest.raises(ConfigError):
        check_bot_config(s)


async def test_button_uses_configured_webapp_url(rig: Rig) -> None:
    other = "https://other.feelings.example/mini?x=1"

    def custom() -> Settings:
        s = a8_settings()
        s.webapp_url = other
        return s

    app.dependency_overrides[get_settings] = custom
    await rig.post(tg_message("/start"))
    await rig.post(tg_message("просто текст"))
    for body in rig.stub.bodies():
        assert web_app_urls(body) == [other]


# ---------------------------------------------------------------------------------------------
# Уведомления без текста сообщения (ФТ-18, §4.4 п.3)
# ---------------------------------------------------------------------------------------------


def test_notify_signature_has_no_free_text() -> None:
    sig = inspect.signature(notify)
    assert list(sig.parameters) == ["api", "chat_id", "kind", "webapp_url"]
    assert sig.parameters["kind"].annotation is NotificationKind
    assert not any(
        p.kind is p.VAR_KEYWORD or p.kind is p.VAR_POSITIONAL for p in sig.parameters.values()
    )


async def test_notify_rejects_arbitrary_kind_string() -> None:
    stub = TgStub()
    api = BotApi(BOT_TOKEN, transport=httpx.MockTransport(stub))
    try:
        with pytest.raises((KeyError, ValueError, TypeError)):
            await notify(api, 1, USER_TEXT, WEBAPP)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            await notify(api, 1, NotificationKind.NEW_MESSAGE, WEBAPP, USER_TEXT)  # type: ignore[call-arg]
    finally:
        await api.aclose()
    assert stub.requests == []


async def test_notify_actual_request_body_is_fixed_template() -> None:
    stub = TgStub()
    api = BotApi(BOT_TOKEN, transport=httpx.MockTransport(stub))
    try:
        for kind in NotificationKind:
            await notify(api, 777, kind, WEBAPP)
    finally:
        await api.aclose()
    bodies = stub.bodies()
    assert stub.methods() == ["sendMessage"] * len(NotificationKind)
    texts = {b["text"] for b in bodies}
    assert len(texts) == len(NotificationKind)  # у каждого вида свой фиксированный текст
    for b in bodies:
        assert set(b) == {"chat_id", "text", "reply_markup"}
        assert b["chat_id"] == 777
        assert_button_to_webapp(b)
        assert "parse_mode" not in b
        assert USER_TEXT not in b["text"]


# =============================================================================================
# Раунд 2 (A8): места, изменённые исправлениями 4b2b7a9 — ожидание исхода первой доставки
# (wait_outcome), общий срок HANDLE_DEADLINE_S, BotApiError.description, try/finally в lifespan.
# ТЗ §4.1 п.1 (вход через бота: ответ на /start не теряется и не дублируется), §9.1 (секреты),
# §9.1 п.5 (журналы без текстов).
# =============================================================================================


async def _wait_requests(stub: TgStub, n: int = 1) -> None:
    for _ in range(500):
        if len(stub.requests) >= n:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("Bot API не вызван")


async def test_many_duplicates_wait_for_successful_first_delivery(rig: Rig) -> None:
    upd = tg_message("/start")
    uid = upd["update_id"]
    rig.stub.gate = asyncio.Event()
    first = asyncio.create_task(rig.post(upd))
    await _wait_requests(rig.stub)
    dups = [asyncio.create_task(rig.post(upd)) for _ in range(50)]
    await asyncio.sleep(0.05)
    assert not any(t.done() for t in dups)  # повторы не подтверждаются раньше первой
    rig.stub.gate.set()
    results = await asyncio.wait_for(asyncio.gather(first, *dups), 5.0)
    assert all(r.status_code == 200 for r in results)
    assert len(rig.stub.bodies()) == 1
    updates: UpdateDeduplicator = app.state.bot_updates
    assert not updates.in_progress(uid)  # событие снято, ожидающих не осталось
    assert len(updates) == 1


async def test_many_duplicates_during_transient_failure_all_503_then_processed(rig: Rig) -> None:
    upd = tg_message("/start")
    rig.stub.gate = asyncio.Event()
    rig.stub.script = [httpx.Response(502, text="Bad Gateway")]
    first = asyncio.create_task(rig.post(upd))
    await _wait_requests(rig.stub)
    dups = [asyncio.create_task(rig.post(upd)) for _ in range(30)]
    # все повторы должны дойти до ожидания раньше исхода первой (опоздавший после снятия
    # отметки законно обрабатывается заново — это не предмет теста)
    await asyncio.sleep(0.3)
    rig.stub.gate.set()
    results = await asyncio.wait_for(asyncio.gather(first, *dups), 5.0)
    assert [r.status_code for r in results] == [503] * 31
    assert len(rig.stub.bodies()) == 1
    # Telegram повторяет — обновление обрабатывается ровно один раз
    assert (await rig.post(upd)).status_code == 200
    assert (await rig.post(upd)).status_code == 200
    assert len(rig.stub.bodies()) == 2


async def test_hanging_bot_api_hits_deadline_for_first_and_duplicate(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(webhook_module, "HANDLE_DEADLINE_S", 0.2)
    upd = tg_message("/start")
    uid = upd["update_id"]
    rig.stub.gate = asyncio.Event()  # Bot API «висит» и не отвечает
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    first = asyncio.create_task(rig.post(upd))
    await _wait_requests(rig.stub)
    r1, r2 = await asyncio.wait_for(asyncio.gather(first, rig.post(upd)), 5.0)
    assert (r1.status_code, r2.status_code) == (503, 503)
    assert loop.time() - t0 < 2.0  # ни одна доставка не висит дольше срока
    assert not app.state.bot_updates.in_progress(uid)
    rig.stub.gate.set()
    assert (await rig.post(upd)).status_code == 200
    assert len(rig.stub.bodies()) == 2


async def test_deadline_cuts_long_429_backoff(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    # Настоящий asyncio.sleep: 429 с retry_after 10 (в пределах max_retry_after) — срок прерывает паузу
    monkeypatch.setattr(webhook_module, "HANDLE_DEADLINE_S", 0.2)
    api = BotApi(BOT_TOKEN, transport=httpx.MockTransport(rig.stub))
    saved = app.state.bot_api
    app.state.bot_api = api
    try:
        rig.stub.script = [tg_error(429, 10)]
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        r = await asyncio.wait_for(rig.post(tg_message("/start")), 5.0)
        assert r.status_code == 503
        assert loop.time() - t0 < 2.0
    finally:
        app.state.bot_api = saved
        await api.aclose()


async def test_cancelled_first_delivery_does_not_block_update_forever(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отмена обработчика первой доставки (CancelledError — не Exception) не должна оставлять
    update_id навсегда «в работе»: иначе каждый повтор ждёт срок и получает 503."""
    monkeypatch.setattr(webhook_module, "HANDLE_DEADLINE_S", 0.2)
    upd = tg_message("/start")
    uid = upd["update_id"]
    rig.stub.gate = asyncio.Event()
    first = asyncio.create_task(rig.post(upd))
    await _wait_requests(rig.stub)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    rig.stub.gate.set()
    assert not app.state.bot_updates.in_progress(uid)
    assert (await asyncio.wait_for(rig.post(upd), 5.0)).status_code == 200


async def test_duplicate_wait_is_bounded_even_if_first_never_finishes(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Первая доставка «зависла» навсегда (например, D-3): повтор всё равно отвечает в срок — 503
    monkeypatch.setattr(webhook_module, "HANDLE_DEADLINE_S", 0.2)
    upd = tg_message("/start")
    assert app.state.bot_updates.claim(upd["update_id"])  # отметка без исхода
    r = await asyncio.wait_for(rig.post(upd), 5.0)
    assert r.status_code == 503
    assert rig.stub.requests == []


def test_deadline_relation_to_client_timeouts_and_telegram_wait() -> None:
    from app.bot.client import DEFAULT_TIMEOUT

    deadline = webhook_module.HANDLE_DEADLINE_S
    connect, read = DEFAULT_TIMEOUT.connect, DEFAULT_TIMEOUT.read
    assert connect is not None and read is not None
    # одна обычная медленная попытка (соединение + чтение) укладывается в срок
    assert deadline > connect + read
    # Telegram ждёт ответа на вебхук порядка минуты: 503 должен успеть дойти
    assert deadline <= 30
    # без срока худший случай клиента (3 попытки по connect+read и 2 паузы по 10 с) — 65 с
    api = BotApi(BOT_TOKEN)
    worst = (api._max_retries + 1) * (connect + read) + api._max_retries * api._max_retry_after
    assert worst > deadline  # срок — действительно действующий предел


# --- UpdateDeduplicator.wait_outcome: модульно ------------------------------------------------


async def test_wait_outcome_unknown_and_completed() -> None:
    d = UpdateDeduplicator()
    assert await d.wait_outcome(1) is False  # отметки нет
    assert d.claim(1)
    d.complete(1)
    assert await asyncio.wait_for(d.wait_outcome(1), 0.5) is True
    d.complete(2)  # завершение незакреплённого не создаёт записи
    d.release(2)
    assert len(d) == 1 and not d.in_progress(2)


async def test_wait_outcome_wakes_all_waiters() -> None:
    d = UpdateDeduplicator()
    for uid, finish, expected in ((1, "complete", True), (2, "release", False)):
        assert d.claim(uid)
        waiters = [asyncio.create_task(d.wait_outcome(uid)) for _ in range(500)]
        await asyncio.sleep(0)
        assert not any(w.done() for w in waiters)
        getattr(d, finish)(uid)
        got = await asyncio.wait_for(asyncio.gather(*waiters), 1.0)
        assert got == [expected] * 500
        assert not d.in_progress(uid)
    # повторное завершение/снятие безвредно
    d.complete(1)
    d.release(2)
    assert len(d) == 1


async def test_wait_outcome_eviction_wakes_waiter_and_late_complete_is_noop() -> None:
    d = UpdateDeduplicator(max_size=2)
    assert d.claim(1)
    waiter = asyncio.create_task(d.wait_outcome(1))
    await asyncio.sleep(0)
    assert d.claim(2) and d.claim(3)  # 1 вытеснено, будучи «в работе»
    assert await asyncio.wait_for(waiter, 0.5) is False
    d.complete(1)  # поздний исход вытесненного не возвращает запись
    assert len(d) == 2 and d.claim(1)


async def test_wait_outcome_reclaimed_after_release_is_not_confirmed() -> None:
    d = UpdateDeduplicator()
    assert d.claim(5)
    waiter = asyncio.create_task(d.wait_outcome(5))
    await asyncio.sleep(0)
    d.release(5)
    assert d.claim(5)  # третья доставка взяла обновление до пробуждения ожидающего
    assert await asyncio.wait_for(waiter, 0.5) is False  # 503: исход ещё неизвестен
    d.complete(5)
    assert await d.wait_outcome(5) is True


# --- BotApiError.description (ревью m6) -------------------------------------------------------


async def _error_for(body: Any, status: int = 400) -> BotApiError:
    stub = TgStub()
    stub.script = [httpx.Response(status, json=body)]
    api = BotApi(BOT_TOKEN, transport=httpx.MockTransport(stub))
    try:
        with pytest.raises(BotApiError) as info:
            await api.call("setWebhook", {"url": "https://x.example"})
    finally:
        await api.aclose()
    return info.value


@pytest.mark.parametrize(
    "description",
    [
        f"Bad Request: bad webhook {BOT_TOKEN}",
        f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook failed",
        "x" * 290 + BOT_TOKEN,  # токен на границе обрезки
        "y" * 5000 + BOT_TOKEN,
        "Ж" * 5000,
    ],
)
async def test_description_redacted_bounded_and_not_in_str(description: str) -> None:
    err = await _error_for({"ok": False, "error_code": 400, "description": description})
    assert err.description is not None
    assert len(err.description) <= 300
    _assert_no_token(err.description)
    assert BOT_ID not in err.description  # нет и обрезка токена
    for text in (str(err), repr(err), repr(err.args), "".join(traceback.format_exception(err))):
        assert err.description not in text
        _assert_no_token(text)
    assert str(err) == "Bot API setWebhook: error_code 400"


@pytest.mark.parametrize("value", [None, 123, {"text": BOT_TOKEN}, [BOT_TOKEN], True])
async def test_non_string_description_is_dropped(value: Any) -> None:
    body: dict[str, Any] = {"ok": False, "error_code": 400}
    if value is not None:
        body["description"] = value
    err = await _error_for(body)
    assert err.description is None


async def test_description_not_logged_by_webhook(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    _all_loggers_debug(caplog)
    marker = "A8-desc-marker: chat not found"
    for code in (400, 403, 502, 429):
        rig.stub.script = [
            httpx.Response(code, json={"ok": False, "error_code": code, "description": marker})
        ]
        await rig.post(tg_message("/start"))
    assert marker not in caplog.text
    assert "A8-desc-marker" not in caplog.text


def test_setup_describe_error_shows_description_without_token() -> None:
    from app.bot.setup import describe_error

    with_desc = BotApiError(
        "setWebhook", "error_code 400", error_code=400, description="bad webhook: ***"
    )
    assert describe_error(with_desc) == "Bot API setWebhook: error_code 400: bad webhook: ***"
    plain = BotApiError("setWebhook", "timeout", transient=True)
    assert describe_error(plain) == "Bot API setWebhook: timeout"


def test_setup_main_exits_with_description_on_bot_api_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import runpy
    import sys

    import app.config as config_module

    async def failing_call(self: BotApi, method: str, payload: dict[str, Any]) -> Any:
        raise BotApiError(
            method,
            "error_code 400",
            error_code=400,
            description="Bad Request: bad webhook: IP address 10.0.0.1 is reserved",
        )

    monkeypatch.setattr(BotApi, "call", failing_call)
    monkeypatch.setattr(config_module, "get_settings", a8_settings)
    monkeypatch.setattr(
        sys, "argv", ["setup", "--webhook-url", "https://hook.feelings.example/api/v1/x"]
    )
    monkeypatch.delitem(sys.modules, "app.bot.setup", raising=False)
    with pytest.raises(SystemExit) as info:
        runpy.run_module("app.bot.setup", run_name="__main__")
    msg = str(info.value.code)
    assert "setChatMenuButton" in msg and "error_code 400" in msg
    assert "IP address 10.0.0.1 is reserved" in msg
    _assert_no_token(msg)
    _assert_no_token(capsys.readouterr().err)


# --- lifespan: try/finally (ревью m9) ---------------------------------------------------------


class _FakeEngine:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def dispose(self) -> None:
        self.log.append("dispose")


def _patch_lifespan(
    monkeypatch: pytest.MonkeyPatch, log: list[str], *, ctor_fails=False, close_fails=False
) -> None:
    import app.main as main_module

    class FakeBotApi:
        def __init__(self, token: Any) -> None:
            if ctor_fails:
                raise RuntimeError("ctor")
            log.append("bot_api")

        async def aclose(self) -> None:
            log.append("aclose")
            if close_fails:
                raise RuntimeError("aclose")

    monkeypatch.setattr(main_module, "get_settings", a8_settings)
    monkeypatch.setattr(main_module, "make_engine", lambda url: _FakeEngine(log))
    monkeypatch.setattr(main_module, "make_sessionmaker", lambda engine: object())
    monkeypatch.setattr(main_module, "BotApi", FakeBotApi)


async def test_lifespan_normal_shutdown_closes_both(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI

    from app.main import lifespan

    log: list[str] = []
    _patch_lifespan(monkeypatch, log)
    async with lifespan(FastAPI()):
        log.append("serve")
    assert log == ["bot_api", "serve", "aclose", "dispose"]


async def test_lifespan_disposes_engine_when_aclose_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI

    from app.main import lifespan

    log: list[str] = []
    _patch_lifespan(monkeypatch, log, close_fails=True)
    with pytest.raises(RuntimeError, match="aclose"):
        async with lifespan(FastAPI()):
            pass
    assert log == ["bot_api", "aclose", "dispose"]


async def test_lifespan_disposes_engine_when_bot_api_ctor_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI

    from app.main import lifespan

    log: list[str] = []
    _patch_lifespan(monkeypatch, log, ctor_fails=True)
    with pytest.raises(RuntimeError, match="ctor"):
        async with lifespan(FastAPI()):
            pass
    assert log == ["dispose"]


async def test_lifespan_body_error_closes_both(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI

    from app.main import lifespan

    log: list[str] = []
    _patch_lifespan(monkeypatch, log)
    with pytest.raises(KeyboardInterrupt):
        async with lifespan(FastAPI()):
            raise KeyboardInterrupt
    assert log == ["bot_api", "aclose", "dispose"]


# --- Доработка 1.4: режим разработки (опрос getUpdates, check, setup --no-webhook) -----------
# ТЗ §9.1 п.5 (токен бота — только в окружении, не в журналах и выводе); карточка 1.4, раздел
# «Живая проверка без сервера». Независимая подмена Bot API (свой стаб), без сети.

import os  # noqa: E402
import signal  # noqa: E402

from app.bot import check as check_module  # noqa: E402
from app.bot import poll as poll_module  # noqa: E402
from app.bot import setup as a8_bot_setup  # noqa: E402
from app.bot.processing import Delivery, process_update  # noqa: E402
from app.config import check_dev_bot_config  # noqa: E402
from app.core.logging import install_log_redaction  # noqa: E402


def a8_dev_settings(**overrides: Any) -> Settings:
    """Как у разработчика без сервера: нет секрета вебхука, секрета сессии и БД."""
    base: dict[str, Any] = {
        "telegram_bot_token": SecretStr(BOT_TOKEN),
        "webapp_url": WEBAPP,
        "bot_username": "FeelingsA8Bot",
        "telegram_webhook_secret": SecretStr(""),
        "session_secret": SecretStr(""),
        "database_url": "postgresql+asyncpg://nobody:none@db.invalid:1/none",
        "env": "dev",
    }
    base.update(overrides)
    return Settings(**base)


_A8_UID = [810_000]


def a8_update(text: str = "/start") -> dict[str, Any]:
    _A8_UID[0] += 1
    return {
        "update_id": _A8_UID[0],
        "message": {
            "message_id": 3,
            "date": 1_700_000_100,
            "chat": {"id": 77_000_002, "type": "private"},
            "from": {"id": 77_000_002, "is_bot": False, "first_name": "Вера"},
            "text": text,
        },
    }


def _ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


class PollStub:
    """getUpdates отвечает по сценарию `updates`; сценарий кончился — `stop` и пустой ответ
    (или зависание, если `hang_when_empty`). sendMessage — по сценарию `sends`, затем успех.
    `on_send` вызывается при каждом sendMessage до ответа."""

    def __init__(self, stop: asyncio.Event | None = None) -> None:
        self.stop = stop
        self.updates: list[Any] = []
        self.sends: list[httpx.Response | Exception] = []
        self.confirm: httpx.Response | Exception | None = None
        self.hang_when_empty = False
        self.log: list[tuple[str, dict[str, Any], Any]] = []  # метод, тело, тайм-аут чтения
        self.on_send: Any = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.log.append((method, body, request.extensions["timeout"]["read"]))
        if method == "getUpdates":
            if body.get("timeout") == 0:
                item = self.confirm if self.confirm is not None else _ok([])
            elif self.updates:
                item = self.updates.pop(0)
            elif self.hang_when_empty:
                await asyncio.Event().wait()
                raise AssertionError("unreachable")
            else:
                if self.stop is not None:
                    self.stop.set()
                return _ok([])
            if isinstance(item, Exception):
                raise item
            return item if isinstance(item, httpx.Response) else _ok(item)
        if method == "sendMessage":
            if self.on_send is not None:
                await self.on_send()
            if self.sends:
                item = self.sends.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
            return _ok({"message_id": 9})
        return _ok(True)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def methods(self) -> list[str]:
        return [m for m, _, _ in self.log]

    def polls(self) -> list[dict[str, Any]]:
        return [b for m, b, _ in self.log if m == "getUpdates"]


async def _a8_run(stub: PollStub, stop: asyncio.Event, **kw: Any) -> int | None:
    settings = kw.pop("settings", None) or a8_dev_settings()
    kw.setdefault("retry_delays", (0.0,))
    return await asyncio.wait_for(
        poll_module.run(settings, stop=stop, transport=stub.transport(), **kw), 5.0
    )


def _clean(*texts: str) -> None:
    for text in texts:
        assert BOT_TOKEN not in text
        assert TOKEN_TAIL not in text


async def test_poll_offset_moves_only_after_update_processed() -> None:
    """Карточка 1.4: offset — только после обработки. Запрос с offset=u+1 уходит позже ответа
    пользователю, а не до него."""
    stop = asyncio.Event()
    stub = PollStub(stop)
    u1, u2 = a8_update(), a8_update(f"/start {PAYLOAD_64}")
    stub.updates = [[u1, u2]]
    offset = await _a8_run(stub, stop)
    seq = [(m, b.get("offset")) for m, b, _ in stub.log]
    assert seq[0] == ("deleteWebhook", None)
    assert seq[1] == ("getUpdates", None)
    assert [m for m, _ in seq[2:4]] == ["sendMessage", "sendMessage"]
    assert seq[4] == ("getUpdates", u2["update_id"] + 1)
    assert offset == u2["update_id"] + 1
    # ни один getUpdates до ответов не подтверждал u1/u2
    assert all(b.get("offset") is None for m, b, _ in stub.log[:4] if m == "getUpdates")


async def test_poll_stop_during_retry_pause_confirms_only_processed() -> None:
    """Временный сбой на u2, затем остановка во время паузы: подтверждён только u1, u2 придёт
    при следующем запуске (без потери)."""
    stop = asyncio.Event()
    stub = PollStub(stop)
    u1, u2 = a8_update(), a8_update()
    stub.updates = [[u1, u2]]
    stub.sends = [
        _ok({"message_id": 1}),
        httpx.Response(503, json={"ok": False, "error_code": 503}),
    ]
    task = asyncio.create_task(_a8_run(stub, stop, retry_delays=(60.0,)))
    for _ in range(200):
        if stub.methods().count("sendMessage") == 2:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)
    assert not task.done()  # ждёт 60 с перед повтором
    stop.set()
    offset = await asyncio.wait_for(task, 2.0)
    assert offset == u1["update_id"] + 1
    confirm = stub.polls()[-1]
    assert confirm["timeout"] == 0 and confirm["offset"] == u1["update_id"] + 1


async def test_poll_junk_items_in_batch_do_not_break_offset() -> None:
    stop = asyncio.Event()
    stub = PollStub(stop)
    u = a8_update()
    stub.updates = [["junk", {"no_id": 1}, {"update_id": True}, {"update_id": "5"}, u]]
    offset = await _a8_run(stub, stop)
    assert offset == u["update_id"] + 1
    assert stub.methods().count("sendMessage") == 1


async def test_poll_unexpected_result_is_ignored_without_crash() -> None:
    stop = asyncio.Event()
    stub = PollStub(stop)
    stub.updates = [{"not": "a list"}]
    offset = await _a8_run(stub, stop)
    assert offset is None
    assert "sendMessage" not in stub.methods()


@pytest.mark.parametrize("code", [401, 404])
async def test_poll_bad_token_exits_without_retry(code: int) -> None:
    stop = asyncio.Event()
    stub = PollStub(stop)
    stub.updates = [httpx.Response(code, json={"ok": False, "error_code": code})]
    with pytest.raises(poll_module.PollStopped) as info:
        await _a8_run(stub, stop, retry_delays=(0.0,))
    assert len(stub.polls()) == 1
    _clean(str(info.value), "".join(traceback.format_exception(info.value)))


async def test_poll_bad_token_on_delete_webhook_main_exit_clean(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stub = PollStub()

    async def reject(request: httpx.Request) -> httpx.Response:
        stub.log.append((request.url.path.rsplit("/", 1)[-1], {}, None))
        return httpx.Response(
            401, json={"ok": False, "error_code": 401, "description": f"bad bot{BOT_TOKEN}"}
        )

    monkeypatch.setattr(
        poll_module,
        "BotApi",
        lambda token, **kw: BotApi(token, transport=httpx.MockTransport(reject)),
    )
    monkeypatch.setattr(poll_module, "get_settings", a8_dev_settings)
    with pytest.raises(SystemExit) as info:
        await asyncio.to_thread(poll_module.main, [])
    out = capsys.readouterr()
    assert "401" in str(info.value.code)
    _clean(str(info.value.code), out.out, out.err)
    assert stub.methods() == ["deleteWebhook"]


async def test_poll_confirm_failure_is_logged_without_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    stop = asyncio.Event()
    stub = PollStub()
    stub.hang_when_empty = True
    u = a8_update()
    stub.updates = [[u]]
    stub.confirm = httpx.ConnectError(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates")
    task = asyncio.create_task(_a8_run(stub, stop))
    for _ in range(200):
        if len(stub.polls()) == 2:
            break
        await asyncio.sleep(0.01)
    stop.set()
    offset = await asyncio.wait_for(task, 2.0)
    assert offset == u["update_id"] + 1
    assert "confirming processed updates failed" in caplog.text
    _clean(caplog.text)


async def test_poll_httpx_request_log_redacted(caplog: pytest.LogCaptureFixture) -> None:
    """Журнал httpx «HTTP Request: POST https://api.telegram.org/bot<токен>/getUpdates» — без
    токена (ТЗ §9.1 п.5)."""
    install_log_redaction()
    caplog.set_level(logging.DEBUG)
    stop = asyncio.Event()
    stub = PollStub(stop)
    stub.updates = [[a8_update(USER_TEXT)]]
    await _a8_run(stub, stop)
    assert "HTTP Request" in caplog.text and "getUpdates" in caplog.text
    _clean(caplog.text)
    assert USER_TEXT not in caplog.text


async def test_poll_long_timeout_only_for_get_updates() -> None:
    stop = asyncio.Event()
    stub = PollStub(stop)
    stub.updates = [[a8_update()]]
    await _a8_run(stub, stop)
    for method, body, read in stub.log:
        if method == "getUpdates" and body.get("timeout") == 25:
            assert read is not None and read > 25
        elif method == "sendMessage":
            assert read == 10.0  # обычный тайм-аут клиента, не 35 с


def _patch_poll_api(monkeypatch: pytest.MonkeyPatch, stub: PollStub) -> None:
    monkeypatch.setattr(
        poll_module, "BotApi", lambda token, **kw: BotApi(token, transport=stub.transport())
    )
    monkeypatch.setattr(poll_module, "get_settings", a8_dev_settings)


async def test_poll_real_sigterm_mid_update_finishes_it_and_confirms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGTERM во время ответа пользователю: ответ дорабатывается, offset подтверждается,
    процесс завершается без отмены."""
    stub = PollStub()
    stub.hang_when_empty = True
    u = a8_update()
    stub.updates = [[u]]
    gate = asyncio.Event()

    async def on_send() -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(0.05)
        gate.set()
        await gate.wait()

    stub.on_send = on_send
    _patch_poll_api(monkeypatch, stub)
    await asyncio.wait_for(asyncio.create_task(poll_module._amain(False)), 3.0)
    assert stub.methods() == ["deleteWebhook", "getUpdates", "sendMessage", "getUpdates"]
    assert stub.polls()[-1]["offset"] == u["update_id"] + 1
    assert stub.polls()[-1]["timeout"] == 0
    assert not asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)


async def test_poll_second_sigterm_cancels_hanging_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = PollStub()
    u = a8_update()
    stub.updates = [[u]]
    reached = asyncio.Event()

    async def on_send() -> None:
        reached.set()
        await asyncio.Event().wait()

    stub.on_send = on_send
    _patch_poll_api(monkeypatch, stub)
    task = asyncio.create_task(poll_module._amain(False))
    await asyncio.wait_for(reached.wait(), 3.0)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.sleep(0.05)
    assert not task.done()  # первый сигнал — ждём окончания обработки
    os.kill(os.getpid(), signal.SIGTERM)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2.0)
    assert not asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)


@pytest.mark.parametrize("env", ["prod", "Production"])
def test_poll_main_refuses_prod_from_environment(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    """FEELINGS_ENV=prod из окружения: опрос не стартует и вебхук продуктива не удаляет."""
    stub = PollStub()
    monkeypatch.setattr(
        poll_module, "BotApi", lambda token, **kw: BotApi(token, transport=stub.transport())
    )
    monkeypatch.setenv("FEELINGS_ENV", env)
    monkeypatch.setenv("FEELINGS_TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("FEELINGS_WEBAPP_URL", WEBAPP)
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as info:
            poll_module.main([])
    finally:
        get_settings.cache_clear()
    assert "development only" in str(info.value.code)
    _clean(str(info.value.code))
    assert stub.log == []


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"telegram_webhook_secret": SecretStr("")}, "WEBHOOK_SECRET"),
        ({"telegram_webhook_secret": SecretStr("a" * 31)}, "WEBHOOK_SECRET"),
        ({"telegram_webhook_secret": SecretStr("a" * 31 + "!")}, "WEBHOOK_SECRET"),
        ({"webapp_url": "http://mini.feelings.example/"}, "WEBAPP_URL"),
        ({"telegram_bot_token": SecretStr("")}, "BOT_TOKEN"),
    ],
)
def test_server_bot_config_not_weakened(override: dict[str, Any], field: str) -> None:
    """check_bot_config сервера (app.main) по-прежнему требует токен, стойкий секрет вебхука и
    https-адрес Mini App; check_dev_bot_config — только токен и адрес."""
    settings = a8_settings().model_copy(update=override)
    with pytest.raises(ConfigError, match=field):
        check_bot_config(settings)
    if field == "WEBHOOK_SECRET":
        check_dev_bot_config(settings)  # опросу секрет не нужен
    check_bot_config(a8_settings())


def test_server_lifespan_uses_server_check() -> None:
    import app.main as main_module

    src = inspect.getsource(main_module)
    assert "check_bot_config(settings)" in src
    assert "check_dev_bot_config" not in src


async def test_process_update_outcomes_match_webhook_contract() -> None:
    """Общий process_update: исходы, которые вебхук превращает в 200/503."""
    api = BotApi(BOT_TOKEN, transport=httpx.MockTransport(lambda r: _ok(True)))
    seen = UpdateDeduplicator()

    async def ok(*_: Any) -> str:
        return "start"

    async def transient(*_: Any) -> str:
        raise BotApiError("sendMessage", "x", error_code=502, transient=True)

    async def permanent(*_: Any) -> str:
        raise BotApiError("sendMessage", "x", error_code=403)

    async def boom(*_: Any) -> str:
        raise RuntimeError(USER_TEXT)

    async def slow(*_: Any) -> str:
        await asyncio.sleep(10)
        return "start"

    cases = [(ok, Delivery.ACK), (transient, Delivery.RETRY), (permanent, Delivery.ACK)]
    cases += [(boom, Delivery.ACK), (slow, Delivery.RETRY)]
    for handler, expected in cases:
        u = a8_update()
        got = await process_update(api, seen, u, WEBAPP, handler=handler, deadline_s=0.05)
        assert got is expected, handler.__name__
        if expected is Delivery.RETRY:
            assert seen.claim(u["update_id"])  # отметка снята: повтор обработается
        else:
            again = await process_update(api, seen, u, WEBAPP, handler=boom, deadline_s=0.05)
            assert again is Delivery.ACK  # дубль
    assert await process_update(api, seen, {"x": 1}, WEBAPP, handler=boom) is Delivery.ACK
    await api.aclose()


class CheckStub:
    def __init__(self, me: httpx.Response, hook: httpx.Response | None = None) -> None:
        self.me, self.hook = me, hook
        self.methods: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.methods.append(method)
        if method == "getMe":
            return self.me
        if method == "getWebhookInfo" and self.hook is not None:
            return self.hook
        return _ok(True)


async def test_check_reads_only_and_hides_id_token_and_webhook() -> None:
    hook_url = f"https://hook-secret-host.example/api/v1/telegram/webhook?k={HOOK_SECRET}"
    me = _ok({"id": int(BOT_ID), "is_bot": True, "first_name": "Ч", "username": "FeelingsA8Bot"})
    hook = _ok(
        {
            "url": hook_url,
            "pending_update_count": 3,
            "ip_address": "203.0.113.9",
            "last_error_message": f"Wrong response {USER_TEXT}",
        }
    )
    stub = CheckStub(me, hook)
    lines = await check_module.check_bot(
        a8_dev_settings(webapp_url=""), transport=httpx.MockTransport(stub)
    )
    text = "\n".join(lines)
    assert stub.methods == ["getMe", "getWebhookInfo"]  # только чтение, вебхук не трогается
    assert "@FeelingsA8Bot" in text and "ожидают доставки: 3" in text
    _clean(text)
    for secret in (BOT_ID, "hook-secret-host", HOOK_SECRET, "203.0.113.9", USER_TEXT):
        assert secret not in text


async def test_check_rejects_non_bot_and_404() -> None:
    me = _ok({"id": 1, "is_bot": False, "username": "human"})
    with pytest.raises(BotApiError):
        await check_module.check_bot(
            a8_dev_settings(), transport=httpx.MockTransport(CheckStub(me))
        )
    gone = httpx.Response(404, json={"ok": False, "error_code": 404, "description": "Not Found"})
    with pytest.raises(check_module.TokenRejected):
        await check_module.check_bot(
            a8_dev_settings(), transport=httpx.MockTransport(CheckStub(gone))
        )


def test_check_main_network_error_has_no_token_or_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connect {request.url}")

    monkeypatch.setattr(
        check_module,
        "BotApi",
        lambda token, **kw: BotApi(token, transport=httpx.MockTransport(fail)),
    )
    monkeypatch.setattr(check_module, "get_settings", a8_dev_settings)
    with pytest.raises(SystemExit) as info:
        check_module.main()
    out = capsys.readouterr()
    _clean(str(info.value.code), out.out, out.err)
    assert "Traceback" not in out.err


class SetupStub:
    def __init__(self) -> None:
        self.methods: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.methods.append(request.url.path.rsplit("/", 1)[-1])
        return _ok(True)


def test_setup_no_webhook_never_touches_webhook_even_with_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stub = SetupStub()
    monkeypatch.setattr(
        a8_bot_setup,
        "BotApi",
        lambda token, **kw: BotApi(token, transport=httpx.MockTransport(stub)),
    )
    monkeypatch.setattr(a8_bot_setup, "get_settings", a8_settings)  # секрет есть, но не нужен
    a8_bot_setup.main(["--no-webhook"])
    assert sorted(stub.methods) == ["setChatMenuButton", "setMyCommands"]
    out = capsys.readouterr()
    _clean(out.out, out.err)
    assert HOOK_SECRET not in out.out + out.err


@pytest.mark.parametrize("webapp", ["", "http://mini.feelings.example/"])
def test_setup_no_webhook_requires_https_webapp(
    monkeypatch: pytest.MonkeyPatch, webapp: str
) -> None:
    stub = SetupStub()
    monkeypatch.setattr(
        a8_bot_setup,
        "BotApi",
        lambda token, **kw: BotApi(token, transport=httpx.MockTransport(stub)),
    )
    monkeypatch.setattr(a8_bot_setup, "get_settings", lambda: a8_dev_settings(webapp_url=webapp))
    with pytest.raises(SystemExit) as info:
        a8_bot_setup.main(["--no-webhook"])
    assert "WEBAPP_URL" in str(info.value.code)
    assert stub.methods == []


@pytest.mark.parametrize("tool", ["poll", "check"])
def test_dev_tools_install_log_redaction(monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    """Утилиты режима разработки сами включают фильтр журналов: журнал httpx с URL Bot API
    (в нём токен) чистится и при запуске из командной строки (ТЗ §9.1 п.5)."""
    from app.core.logging import RedactInviteTokens

    for name in ("httpx", "app.bot"):
        lg = logging.getLogger(name)
        for f in [f for f in lg.filters if isinstance(f, RedactInviteTokens)]:
            monkeypatch.setattr(lg, "filters", [x for x in lg.filters if x is not f])
    if tool == "poll":

        async def fake_amain(drop_pending: bool) -> None:
            return None

        monkeypatch.setattr(poll_module, "_amain", fake_amain)
        poll_module.main([])
    else:

        async def fake_check(settings: Settings, **kw: Any) -> list[str]:
            return ["ok"]

        monkeypatch.setattr(check_module, "check_bot", fake_check)
        monkeypatch.setattr(check_module, "get_settings", a8_dev_settings)
        check_module.main()
    for name in ("httpx", "app.bot"):
        assert any(isinstance(f, RedactInviteTokens) for f in logging.getLogger(name).filters)

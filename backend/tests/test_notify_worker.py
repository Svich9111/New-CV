"""3.5 (A5): воркер уведомлений через бота — ``app.workers.notify``.

ФТ-18; ТЗ §4.4 п.3 («уведомление в Telegram без текста сообщения»), §9.1 п.5, §10 («до 30 с от
отправки до уведомления»); АР-44; BACKLOG Б-47.

Без сети: Bot API подменён ``httpx.MockTransport`` (``FakeTg``), модель конвейера — заглушка
(``test_pipeline_worker.Model``). Данные выдуманные: пара — функциями БД (``_pair``), воркеры — под
``feelings_worker`` (``SET LOCAL ROLE`` от суперпользователя тестов; чат получателя — в его
``user_scope``). Перед каждым тестом чужие строки очередей «паркуются» долгой арендой."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bot.client import BotApi, BotApiError
from app.bot.notifications import NotificationKind
from app.config import ConfigError, Settings, check_notify_config
from app.core.logging import install_log_redaction
from app.db import make_sessionmaker
from app.repositories import notify_work as nw
from app.workers.notify import NotifyOutcome, NotifySettings, NotifyWorker
from tests.test_couple_functions import LEAVE, SM, _call
from tests.test_pipeline_db import P, _expire, _msg, _pair, _park
from tests.test_pipeline_worker import CALM, Model, send
from tests.test_pipeline_worker import worker as pipeline_worker

TOKEN = "987654321:AAFakeNotifyTokenA5xxxxxxxxxxxxxxxxxx"
WEBAPP = "https://feelings.example/app/"
SECRET_TEXT = "Секретная фраза Анны про отпуск в Сочи"
TG_DESCRIPTION = "Forbidden: bot was blocked by the user DESCR-A5"
NEW_MESSAGE_TEXT = "Партнёр прислал вам сообщение. Откройте приложение, чтобы его прочитать."
ROOT = Path(__file__).resolve().parents[2]

Action = Callable[[httpx.Request], Awaitable[httpx.Response] | httpx.Response] | httpx.Response


def tg_error(code: int, retry_after: int | None = None) -> httpx.Response:
    body: dict[str, Any] = {"ok": False, "error_code": code, "description": TG_DESCRIPTION}
    if retry_after is not None:
        body["parameters"] = {"retry_after": retry_after}
    return httpx.Response(code, json=body)


class FakeTg:
    """Подменённый Bot API: ``script`` — исходы очередных запросов (ответ или исключение
    httpx), дальше — успех. ``sent`` — тела запросов, на которые Telegram ответил «ok»."""

    def __init__(self, *script: Any, delay_s: float = 0.0) -> None:
        self.script = list(script)
        self.delay_s = delay_s
        self.requests: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.got_first = asyncio.Event()

    async def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendMessage")
        body = json.loads(request.content)
        self.requests.append(body)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.script:
            action = self.script.pop(0)
            if isinstance(action, type) and issubclass(action, Exception):
                raise action("simulated", request=request)
            if isinstance(action, httpx.Response):
                if action.status_code == 200:
                    self.sent.append(body)
                return action
        self.sent.append(body)
        self.got_first.set()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})

    def api(self) -> BotApi:
        # Как в процессе воркера: без собственных повторов 429 — паузу держит воркер.
        return BotApi(TOKEN, transport=httpx.MockTransport(self.handler), max_retries=0)


class Sleeps:
    """Подменённый ``sleep`` воркера: записывает паузы, не ждёт."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)
        await asyncio.sleep(0)


def nworker(engine: AsyncEngine, tg: FakeTg, *, webapp: str = WEBAPP, **cfg: Any) -> NotifyWorker:
    sleep = cfg.pop("sleep", None) or Sleeps()
    settings = NotifySettings(**({"retry_pause_s": 0.01} | cfg))
    return NotifyWorker(
        worker_sm=make_sessionmaker(engine),
        api=tg.api(),
        webapp_url=webapp,
        settings=settings,
        sleep=sleep,
    )


async def chat_of(engine: AsyncEngine, uid: uuid.UUID) -> int:
    async with engine.connect() as c:
        return int(
            (
                await c.execute(text("SELECT telegram_id FROM users WHERE id = :u"), {"u": uid})
            ).scalar_one()
        )


async def delivered(sm: SM, engine: AsyncEngine, who: P, body: str = CALM) -> uuid.UUID:
    """Отправка и конвейер (заглушка модели) → сообщение ``delivered`` в очереди уведомлений."""
    mid = await send(sm, who, body)
    out = await pipeline_worker(engine, Model()).run_once()
    assert [o.status for o in out] == ["delivered"]
    return mid


def statuses(out: list[NotifyOutcome]) -> list[tuple[str, str | None]]:
    return [(o.status, o.reason) for o in out]


@pytest.fixture
async def pair(sessionmaker: SM, engine: AsyncEngine) -> tuple[P, P, uuid.UUID]:
    await _park(engine)
    return await _pair(sessionmaker, engine)


def assert_notification(body: dict[str, Any], chat_id: int, *, button: bool = True) -> None:
    """ТЗ §4.4 п.3: фиксированный текст без содержания, имени автора и id; кнопка Mini App."""
    assert body["chat_id"] == chat_id
    assert body["text"] == NEW_MESSAGE_TEXT
    if button:
        assert set(body) == {"chat_id", "text", "reply_markup"}
        assert body["reply_markup"] == {
            "inline_keyboard": [[{"text": "Открыть", "web_app": {"url": WEBAPP}}]]
        }
    else:
        assert set(body) == {"chat_id", "text"}


# --------------------------------------------------------------------------- основной путь


async def test_full_path_send_pipeline_notify_within_30s(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID]
) -> None:
    """§10: от «Отправить» до уведомления ≤ 30 с — оба воркера в непрерывном цикле (опрос
    очереди — 1 с, как по умолчанию), модель — заглушка. Получателю — одно уведомление."""
    anna, igor, _ = pair
    tg = FakeTg()
    nw_ = nworker(engine, tg)
    pw_ = pipeline_worker(engine, Model())
    stop = asyncio.Event()
    loops = [
        asyncio.create_task(pw_.run_forever(poll_s=1.0, stop=stop)),
        asyncio.create_task(nw_.run_forever(poll_s=1.0, stop=stop)),
    ]
    try:
        t0 = time.monotonic()
        mid = await send(sessionmaker, anna, CALM)
        await asyncio.wait_for(tg.got_first.wait(), timeout=30)
        elapsed = time.monotonic() - t0
    finally:
        stop.set()
        await asyncio.gather(*loops)
        await nw_.api.aclose()
    assert elapsed <= 30
    assert len(tg.sent) == 1
    assert_notification(tg.sent[0], await chat_of(engine, igor.user_id))
    row = await _msg(engine, mid)
    assert row is not None and row["notify_done_at"] is not None
    assert row["work_lease_token"] is None


async def test_notification_has_no_message_content_name_or_ids(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID]
) -> None:
    """ФТ-18, ТЗ §4.4 п.3: в запросе к Telegram нет текста сообщения, имени автора, id сообщения
    и пользователей — только чат получателя, фиксированный текст и кнопка."""
    anna, igor, _ = pair
    mid = await delivered(sessionmaker, engine, anna, SECRET_TEXT)
    tg = FakeTg()
    w = nworker(engine, tg)
    assert statuses(await w.run_once()) == [("sent", None)]
    await w.api.aclose()
    assert len(tg.requests) == 1
    raw = json.dumps(tg.requests[0], ensure_ascii=False)
    for secret in (SECRET_TEXT, "Сочи", "Анна", "Игорь", str(mid), str(anna.user_id),
                   str(igor.user_id), str(anna.agent_id)):  # fmt: skip
        assert secret not in raw
    assert_notification(tg.requests[0], await chat_of(engine, igor.user_id))


async def test_no_webapp_url_no_button(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID]
) -> None:
    anna, igor, _ = pair
    await delivered(sessionmaker, engine, anna)
    tg = FakeTg()
    w = nworker(engine, tg, webapp="")
    assert statuses(await w.run_once()) == [("sent", None)]
    await w.api.aclose()
    assert_notification(tg.sent[0], await chat_of(engine, igor.user_id), button=False)


# --------------------------------------------------------------------------- ровно одно


async def test_two_workers_exactly_one_notification_each(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID]
) -> None:
    """Две копии воркера одновременно (медленный Telegram): каждое сообщение — ровно одно
    уведомление; повторный проход ничего не шлёт."""
    anna, igor, _ = pair
    mids = [await send(sessionmaker, anna, f"{CALM} {i}") for i in range(4)]
    mids.append(await send(sessionmaker, igor, CALM))
    out = await pipeline_worker(engine, Model()).run_once()
    assert [o.status for o in out] == ["delivered"] * 5
    tg = FakeTg(delay_s=0.2)
    a, b = nworker(engine, tg, batch=3), nworker(engine, tg, batch=3)
    first = await asyncio.gather(a.run_once(), b.run_once(), a.run_once(), b.run_once())
    got = [o for batch in first for o in batch]
    assert sorted(o.status for o in got) == ["sent"] * 5
    assert await a.run_once() == [] and await b.run_once() == []
    await a.api.aclose()
    await b.api.aclose()
    chats = sorted(body["chat_id"] for body in tg.sent)
    ig, an = await chat_of(engine, igor.user_id), await chat_of(engine, anna.user_id)
    assert chats == sorted([ig] * 4 + [an])
    for m in mids:
        row = await _msg(engine, m)
        assert row is not None and row["notify_done_at"] is not None


async def test_crash_before_send_retried_once_after_lease(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID]
) -> None:
    """Воркер «упал» после захвата: до истечения аренды уведомление никому не выдаётся, после —
    ровно одно (попытка 2)."""
    anna, _igor, _ = pair
    mid = await delivered(sessionmaker, engine, anna)
    tg = FakeTg()
    w = nworker(engine, tg)
    lost = await nw.claim(w.sm, lease=timedelta(minutes=2), max_attempts=5, limit=10)
    assert [c.message_id for c in lost] == [mid]
    assert await w.run_once() == []  # аренда держится
    await _expire(engine, mid)
    out = await w.run_once()
    assert [(o.status, o.attempts) for o in out] == [("sent", 2)]
    assert await w.run_once() == []
    await w.api.aclose()
    assert len(tg.sent) == 1


async def test_stale_token_cannot_mark_after_takeover(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID]
) -> None:
    """Аренду перехватила другая копия: отметка по старому токену — ``False`` (ничего не
    изменено), по новому — проходит; повтор отметки — ``False``."""
    anna, _igor, _ = pair
    mid = await delivered(sessionmaker, engine, anna)
    sm = make_sessionmaker(engine)
    (old,) = await nw.claim(sm, lease=timedelta(minutes=2), max_attempts=5, limit=10)
    await _expire(engine, mid)
    (new,) = await nw.claim(sm, lease=timedelta(minutes=2), max_attempts=5, limit=10)
    assert await nw.done(sm, old) is False
    assert await nw.done(sm, new) is True
    assert await nw.done(sm, new) is False
    assert "message" not in repr(new) and str(mid) not in repr(new)


async def test_mark_write_retried_on_transient_db_error(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """Отметка после отправки упала временной ошибкой БД — повтор той же отметки, второго
    уведомления нет."""
    anna, _igor, _ = pair
    mid = await delivered(sessionmaker, engine, anna)
    real = nw.done
    calls = {"n": 0}

    async def flaky(sm: Any, c: Any) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionResetError("db gone")
        return await real(sm, c)

    monkeypatch.setattr(nw, "done", flaky)
    tg = FakeTg()
    w = nworker(engine, tg)
    assert statuses(await w.run_once()) == [("sent", None)]
    await _expire(engine, mid)
    assert await w.run_once() == []
    await w.api.aclose()
    assert calls["n"] == 2 and len(tg.sent) == 1


# --------------------------------------------------------------------------- ошибки Telegram


@pytest.mark.parametrize(
    "failure", [httpx.ConnectError, httpx.ConnectTimeout, tg_error(502), tg_error(500)]
)
async def test_network_or_5xx_retried_within_lease(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID], failure: Any
) -> None:
    """Сеть (запрос не дошёл) и 5xx — повтор с паузой внутри аренды; одно уведомление."""
    anna, igor, _ = pair
    await delivered(sessionmaker, engine, anna)
    tg = FakeTg(failure, failure)
    sleeps = Sleeps()
    w = nworker(engine, tg, sleep=sleeps)
    assert statuses(await w.run_once()) == [("sent", None)]
    await w.api.aclose()
    assert len(tg.requests) == 3 and len(tg.sent) == 1
    assert sleeps.calls == [0.01, 0.02]  # растущая пауза
    assert tg.sent[0]["chat_id"] == await chat_of(engine, igor.user_id)


async def test_persistent_network_failure_left_to_lease_then_exhausted(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID]
) -> None:
    """Сеть недоступна всё окно — без отметки (повтор после аренды, попытка засчитана БД);
    исчерпание попыток — отметка БД без отправки."""
    anna, _igor, _ = pair
    mid = await delivered(sessionmaker, engine, anna)
    tg = FakeTg(*([httpx.ConnectError] * 4))
    w = nworker(engine, tg, max_sends=2, max_attempts=2)
    assert statuses(await w.run_once()) == [("retry", "network")]
    row = await _msg(engine, mid)
    assert row is not None and row["notify_done_at"] is None and row["work_lease_token"]
    await _expire(engine, mid)
    assert [(o.status, o.attempts) for o in await w.run_once()] == [("retry", 2)]
    await _expire(engine, mid)
    assert await w.run_once() == []  # попытки исчерпаны — отметку ставит БД
    await w.api.aclose()
    row = await _msg(engine, mid)
    assert row is not None and row["notify_done_at"] is not None
    assert tg.sent == [] and len(tg.requests) == 4


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.RemoteProtocolError])
async def test_maybe_sent_not_repeated(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID], failure: Any
) -> None:
    """Тайм-аут чтения / обрыв после отправки — уведомление могло уйти: отметка без повтора
    (дубль хуже пропуска; сообщение видно в ленте)."""
    anna, _igor, _ = pair
    mid = await delivered(sessionmaker, engine, anna)
    tg = FakeTg(failure)
    w = nworker(engine, tg)
    assert statuses(await w.run_once()) == [("unknown", "maybe_sent")]
    await _expire(engine, mid)
    assert await w.run_once() == []
    await w.api.aclose()
    assert len(tg.requests) == 1


async def test_429_pauses_worker_and_retries_after_retry_after(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID]
) -> None:
    """429: пауза на ``retry_after`` (не раньше), затем повтор той же отправки в пределах
    аренды — одно уведомление; пока пауза — новые не захватываются."""
    anna, _igor, _ = pair
    await delivered(sessionmaker, engine, anna)
    tg = FakeTg(tg_error(429, retry_after=7))
    sleeps = Sleeps()
    w = nworker(engine, tg, sleep=sleeps)
    assert statuses(await w.run_once()) == [("sent", None)]
    assert len(sleeps.calls) == 1 and 6.0 < sleeps.calls[0] <= 7.0
    assert len(tg.requests) == 2 and len(tg.sent) == 1
    # Пауза (фальшивый sleep не ждал) ещё действует: новый захват не идёт.
    assert w.paused
    await delivered(sessionmaker, engine, anna)
    assert await w.run_once() == []
    await w.api.aclose()
    assert len(tg.sent) == 1


async def test_429_longer_than_lease_left_to_lease(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID]
) -> None:
    """``retry_after`` дольше окна аренды — без отметки и без ожидания: повтор после аренды."""
    anna, _igor, _ = pair
    mid = await delivered(sessionmaker, engine, anna)
    tg = FakeTg(tg_error(429, retry_after=600))
    sleeps = Sleeps()
    w = nworker(engine, tg, sleep=sleeps)
    assert statuses(await w.run_once()) == [("retry", "http_429")]
    assert sleeps.calls == [] and w.paused
    row = await _msg(engine, mid)
    assert row is not None and row["notify_done_at"] is None
    await w.api.aclose()


@pytest.mark.parametrize(("code", "status"), [(403, "blocked"), (400, "rejected")])
async def test_blocked_or_chat_not_found_marked_without_retry(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID], code: int, status: str,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """403 (бот заблокирован) и 400 (чат не найден) — отметка, повторов нет; 403 — штатный
    исход без ошибки в журнале."""
    anna, _igor, _ = pair
    mid = await delivered(sessionmaker, engine, anna)
    tg = FakeTg(tg_error(code))
    w = nworker(engine, tg)
    with caplog.at_level(logging.INFO):
        assert statuses(await w.run_once()) == [(status, f"http_{code}")]
    await _expire(engine, mid)
    assert await w.run_once() == []
    await w.api.aclose()
    assert len(tg.requests) == 1 and tg.sent == []
    row = await _msg(engine, mid)
    assert row is not None and row["notify_done_at"] is not None
    if code == 403:
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("code", [401, 404])
async def test_bad_bot_token_pauses_without_mark(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID], code: int
) -> None:
    """Неверный токен бота — ошибка настройки: пауза воркера, отметки нет (повтор после аренды)."""
    anna, _igor, _ = pair
    mid = await delivered(sessionmaker, engine, anna)
    tg = FakeTg(tg_error(code))
    w = nworker(engine, tg)
    assert statuses(await w.run_once()) == [("paused", f"http_{code}")]
    assert w.paused and await w.run_once() == []
    await w.api.aclose()
    row = await _msg(engine, mid)
    assert row is not None and row["notify_done_at"] is None


# --------------------------------------------------------------------------- получатель


async def test_recipient_deleted_before_claim_nothing_sent(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID],
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """Получатель удалил аккаунт (ФТ-21) — уведомлять некого: ничего не отправлено, без ошибок."""
    anna, igor, _ = pair
    await delivered(sessionmaker, engine, anna)
    await _call(sessionmaker, igor.user_id, "SELECT app_delete_account()")
    tg = FakeTg()
    w = nworker(engine, tg)
    with caplog.at_level(logging.INFO):
        assert await w.run_once() == []
    await w.api.aclose()
    assert tg.requests == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_recipient_gone_between_claim_and_send(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID]
) -> None:
    """Чат ищется в момент отправки (Б-47): получатель помечен удалённым после захвата —
    ничего не отправлено, отметка ``no_chat``."""
    anna, igor, _ = pair
    mid = await delivered(sessionmaker, engine, anna)
    tg = FakeTg()
    w = nworker(engine, tg)
    (c,) = await nw.claim(w.sm, lease=timedelta(minutes=2), max_attempts=5, limit=10)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": igor.user_id}
        )
    out = await w.handle(c)
    await w.api.aclose()
    assert (out.status, out.reason) == ("no_chat", None)
    assert tg.requests == []
    row = await _msg(engine, mid)
    assert row is not None and row["notify_done_at"] is not None


async def test_dissolved_couple_not_notified(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID]
) -> None:
    """Пара распалась до уведомления — обмен прекращён (ТЗ §4.5 п.2): уведомления нет."""
    anna, igor, _ = pair
    mid = await delivered(sessionmaker, engine, anna)
    await _call(sessionmaker, igor.user_id, LEAVE)
    tg = FakeTg()
    w = nworker(engine, tg)
    assert await w.run_once() == []
    await w.api.aclose()
    assert tg.requests == []
    row = await _msg(engine, mid)
    assert row is not None and row["notify_done_at"] is not None


# --------------------------------------------------------------------------- журналы


async def test_logs_have_no_texts_token_chat_or_ids(
    sessionmaker: SM, engine: AsyncEngine, pair: tuple[P, P, uuid.UUID],
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """ТЗ §9.1 п.5: в журналах воркера и httpx нет текста сообщения, токена бота, chat_id, id
    пользователей и сообщений, описания ошибки от Telegram — на всех исходах."""
    install_log_redaction()  # как в процессе воркера: токен в URL Bot API (журнал httpx)
    anna, igor, _ = pair
    mids = [await send(sessionmaker, anna, f"{SECRET_TEXT} {i}") for i in range(6)]
    pw_ = pipeline_worker(engine, Model())
    assert len(await pw_.run_once()) + len(await pw_.run_once()) == 6
    tg = FakeTg(
        tg_error(403), tg_error(400), httpx.ReadTimeout, httpx.ConnectError, tg_error(502),
        tg_error(429, retry_after=1),
    )  # fmt: skip
    w = nworker(engine, tg, batch=1)
    with caplog.at_level(logging.DEBUG):
        for _ in range(8):
            await w.run_once()
            w._paused_until = 0.0  # фальшивый sleep не ждёт: пауза 429 — снять вручную
    await w.api.aclose()
    assert len(tg.sent) >= 2
    logged = "\n".join(
        f"{r.name} {r.getMessage()}" for r in caplog.records if r.name.startswith(("app", "httpx"))
    )
    assert "notify message: status=blocked" in logged
    secrets = [SECRET_TEXT, "Сочи", TOKEN, TOKEN.split(":")[1], TG_DESCRIPTION, "DESCR-A5"]
    secrets += [str(await chat_of(engine, u)) for u in (anna.user_id, igor.user_id)]
    secrets += [str(x) for x in (anna.user_id, igor.user_id, *mids)]
    for s in secrets:
        assert s not in logged, s


# --------------------------------------------------------------------------- клиент и настройка


@pytest.mark.parametrize(
    ("exc", "maybe"),
    [
        (httpx.ConnectError, False),
        (httpx.ConnectTimeout, False),
        (httpx.PoolTimeout, False),
        (httpx.ReadTimeout, True),
        (httpx.WriteTimeout, True),
        (httpx.ReadError, True),
        (httpx.RemoteProtocolError, True),
    ],
)
async def test_client_marks_maybe_sent(exc: type[Exception], maybe: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc("simulated", request=request)  # type: ignore[call-arg]

    api = BotApi(TOKEN, transport=httpx.MockTransport(handler), max_retries=0)
    try:
        with pytest.raises(BotApiError) as ei:
            await api.send_message(1, "x")
    finally:
        await api.aclose()
    assert ei.value.transient and ei.value.maybe_sent is maybe
    assert TOKEN not in str(ei.value)


def test_notify_config_checks() -> None:
    base = {"telegram_bot_token": TOKEN, "worker_database_url": "postgresql+asyncpg://w@h/d"}
    check_notify_config(Settings(**base))  # без адреса Mini App — допустимо (без кнопки)
    check_notify_config(Settings(**base, webapp_url=WEBAPP))
    for bad in (
        {"telegram_bot_token": ""},
        {"worker_database_url": ""},
        {"webapp_url": "http://feelings.example/"},
    ):
        with pytest.raises(ConfigError) as ei:
            check_notify_config(Settings(**(base | bad)))
        assert TOKEN not in str(ei.value)


def test_notification_kind_text_is_fixed() -> None:
    """Текст нового сообщения — без имени автора и содержания (решение 3.5)."""
    from app.bot.notifications import _TEXTS

    assert _TEXTS[NotificationKind.NEW_MESSAGE] == NEW_MESSAGE_TEXT


def test_compose_services() -> None:
    """Сервис worker-notify в compose разработки и стенда: команда, health check, роль воркера;
    на стенде токен бота обязателен (``:?``), ключей шифрования и OpenRouter у него нет."""
    dev = yaml.safe_load((ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8"))
    raw = (ROOT / "infra" / "deploy" / "docker-compose.prod.yml").read_text(encoding="utf-8")
    prod = yaml.safe_load(raw)
    for compose in (dev, prod):
        svc = compose["services"]["worker-notify"]
        assert svc["command"] == ["python", "-m", "app.workers.notify"]
        assert svc["healthcheck"]["test"]
        assert "feelings_worker_login" in svc["environment"]["FEELINGS_WORKER_DATABASE_URL"]
    env = prod["services"]["worker-notify"]["environment"]
    assert set(env) == {
        "FEELINGS_ENV",
        "FEELINGS_WORKER_DATABASE_URL",
        "FEELINGS_TELEGRAM_BOT_TOKEN",
        "FEELINGS_WEBAPP_URL",
    }
    block = raw.split("  worker-notify:", 1)[1].split("\n\n", 1)[0]
    assert "${FEELINGS_TELEGRAM_BOT_TOKEN:?" in block
    assert prod["services"]["worker-notify"]["networks"] == ["data", "egress"]

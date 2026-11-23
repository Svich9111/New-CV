"""Фоновый воркер уведомлений через бота (ФТ-18; задача 3.5; ТЗ §4.4 п.3, §9.1 п.5, §10; Б-47).

Цикл одного уведомления (БД — ``repositories.notify_work``):

1. **Захват** (``pipeline_claim_notify``, короткая транзакция под ``feelings_worker``): доставленное
   сообщение без отметки, id получателя, токен аренды. ``SKIP LOCKED`` — две копии воркера не
   берут одно сообщение; брошенное (воркер упал, аренда истекла) выдаётся снова. Пара распалась,
   получатель удалён, попытки исчерпаны — отметку без выдачи ставит БД.
2. **Отправка** (``_attempt``): чат Telegram ищется в момент отправки, в области самого
   получателя (``user_scope``, RLS); удалён — ничего не отправляется (Б-47). Текст — фиксированный
   шаблон ``NotificationKind.NEW_MESSAGE`` («Партнёр прислал вам сообщение…»): ни текста
   сообщения, ни имени автора, ни id (ТЗ §4.4 п.3); кнопка «Открыть» — Mini App
   (``FEELINGS_WEBAPP_URL``; не задан — без кнопки).
3. **Отметка** (``pipeline_notify_done`` по своему токену) — при временной ошибке БД повторяется
   до конца аренды (запись по токену идемпотентна).

Ошибки Telegram (``BotApiError``):

- **429** — пауза всего воркера на ``retry_after`` (лимит общий на бота): новые не захватываются,
  начатые ждут и повторяют отправку в пределах своей аренды; пауза дольше окна — аренда снимается
  досрочно с отсрочкой до конца паузы, попытка не расходуется (``pipeline_notify_release``, 0015);
- **403** (бот заблокирован, пользователь деактивирован) и **400** (чат не найден и т. п.) —
  отметка без повторов: повтор ничего не изменит. 403 — штатный исход (INFO);
- **401/404** (токен бота неверен) — ошибка настройки: пауза воркера, отметки нет; аренда снимается
  с отсрочкой на паузу без расхода попытки — уведомления ждут исправления токена, а не
  помечаются сделанными через ``max_attempts`` пауз (ревью 3.5 р.1 MINOR-1);
- **сеть, 5xx** — повтор с растущей паузой в пределах аренды, затем — после её истечения. Только
  если запрос точно не дошёл до Telegram (соединение не установлено) или Telegram ответил
  ошибкой. Тайм-аут чтения или обрыв после отправки (``maybe_sent``) — уведомление могло уйти:
  отметка без повтора (``unknown``). Карточка требует «ровно одно уведомление»: у ``sendMessage``
  нет ключа идемпотентности, и дубль здесь хуже пропуска — сообщение и так видно в ленте Mini App.

Предел попыток — в БД (``pipeline_claim_notify``: ``work_attempts ≥ p_max_attempts`` — отметка без
выдачи). Попытку расходуют только сбои самого уведомления (сеть, 5xx, падение процесса); 429
дольше окна и ошибка настройки бота — снятие аренды без расхода (``nw.release``).

Остановка процесса (SIGTERM/SIGINT — ``docker stop``, выкладка; ``app.workers.signals``): новые
уведомления не захватываются, повторы и ожидание паузы не начинаются (аренда истечёт — повтор
другим процессом), начатый запрос к Telegram дорабатывается и отмечается. Время на это —
``stop_grace_period`` сервиса в compose (не меньше тайм-аута запроса Bot API и отметки).

Остаточный риск «ровно одного»: процесс убит SIGKILL (или БД недоступна дольше аренды) между
успешной отправкой и отметкой — после истечения аренды уведомление уйдёт ещё раз.

Журнал — только исход, вид ошибки (код HTTP или класс исключения) и номер попытки: ни текстов, ни
токена бота, ни chat_id, ни id пользователей и сообщений (ТЗ §9.1 п.5); описание ошибки от
Telegram не пишется.

Запуск: ``python -m app.workers.notify`` — отдельный процесс, а не часть воркера конвейера: ему
нужен токен бота и выход к Telegram, но не нужны ключи шифрования и ключ OpenRouter (и наоборот —
каждый процесс держит только свои секреты); лимиты и сбои Telegram не останавливают конвейер, а
паузы модели — уведомления. БД — ``FEELINGS_WORKER_DATABASE_URL`` (роль входа — член
``feelings_worker`` и ``feelings_app``, Б-69), токен — ``FEELINGS_TELEGRAM_BOT_TOKEN``."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.client import BotApi, BotApiError
from app.bot.notifications import NotificationKind, notify
from app.repositories import notify_work as nw
from app.repositories import users
from app.repositories.notify_work import NotifyClaim
from app.workers.pipeline import transient_db_error
from app.workers.signals import stop_on_signals

logger = logging.getLogger(__name__)

Status = Literal[
    "sent",  # отправлено и отмечено
    "no_chat",  # получатель удалён к моменту отправки — отмечено без отправки (Б-47)
    "blocked",  # 403: бот заблокирован / пользователь деактивирован — отмечено без повторов
    "rejected",  # 400 и прочие постоянные ошибки Telegram — отмечено без повторов
    "unknown",  # запрос мог дойти (тайм-аут чтения, обрыв) — отмечено без повтора
    "retry",  # временный сбой — повтор после истечения аренды
    "paused",  # ошибка настройки бота (401/404) — пауза, отсрочка без расхода попытки
    "lost",  # отметка не своя: аренду перехватили или отметка уже стоит
    "error",  # сбой БД / непредвиденная ошибка — повтор после истечения аренды
]
_MARKED = frozenset({"sent", "no_chat", "blocked", "rejected", "unknown"})
_INFO = frozenset({"sent", "no_chat", "blocked"})

CONFIG_ERROR_CODES = frozenset({401, 404})  # неверный токен бота — лечит оператор
UNREACHABLE_CODES = frozenset({403})  # бот заблокирован, пользователь деактивирован


@dataclass(frozen=True)
class NotifySettings:
    lease: timedelta = timedelta(minutes=2)
    max_attempts: int = 5
    batch: int = 10
    # Запас до конца аренды: отправка заканчивается не позже ``lease − margin`` (остаток —
    # на отметку), иначе другая копия могла бы захватить и отправить второе уведомление.
    margin: timedelta = timedelta(seconds=30)
    # Повтор после сетевого сбоя/5xx внутри аренды: первая пауза, рост ×2 до предела.
    retry_pause_s: float = 1.0
    retry_max_pause_s: float = 10.0
    # Отправок одного уведомления за один захват (429 и временные сбои) — не больше.
    max_sends: int = 5
    # 429 без retry_after — пауза по умолчанию; retry_after сверх предела — предел.
    default_retry_after_s: float = 1.0
    max_retry_after_s: float = 3600.0
    # Пауза воркера при ошибке настройки бота (401/404).
    pause: timedelta = timedelta(minutes=5)

    @property
    def send_window_s(self) -> float:
        lease_s = self.lease.total_seconds()
        margin_s = self.margin.total_seconds()
        return lease_s - margin_s if lease_s > margin_s else lease_s / 2

    @property
    def mark_window_s(self) -> float:
        """Отметку можно повторять почти до конца аренды (запас 5 с)."""
        lease_s = self.lease.total_seconds()
        return max(lease_s - 5.0, self.send_window_s)


@dataclass(frozen=True)
class NotifyOutcome:
    status: Status
    reason: str | None = None  # вид ошибки — код HTTP или класс исключения, без текста
    attempts: int = 0


class _Sent(Exception):
    """Внутренний сигнал: исход отправки известен — отметить."""

    def __init__(self, status: Status, reason: str | None = None) -> None:
        self.status = status
        self.reason = reason


class _Leave(Exception):
    """Внутренний сигнал: не отмечать — повтор после истечения аренды. ``release_after`` —
    снять аренду досрочно с отсрочкой (секунды) без расхода попытки: причина не в уведомлении
    (429 дольше окна, ошибка настройки бота)."""

    def __init__(
        self, status: Status, reason: str | None, *, release_after: float | None = None
    ) -> None:
        self.status = status
        self.reason = reason
        self.release_after = release_after


class NotifyWorker:
    """``worker_sm`` — подключение, которое может ``SET ROLE feelings_worker`` и ``feelings_app``
    (роль входа воркера, Б-69). ``api`` — клиент Bot API без собственных повторов 429
    (``max_retries=0``): паузу держит воркер. ``sleep`` — для тестов."""

    def __init__(
        self,
        *,
        worker_sm: async_sessionmaker[AsyncSession],
        api: BotApi,
        webapp_url: str = "",
        settings: NotifySettings | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.sm = worker_sm
        self.api = api
        self.webapp_url = webapp_url
        self.settings = settings or NotifySettings()
        self._sleep = sleep
        self._paused_until = 0.0  # time.monotonic()
        self._stop: asyncio.Event | None = None  # остановка процесса (``run_forever``)

    # ----------------------------------------------------------------- пауза (429, настройка)

    @property
    def paused(self) -> bool:
        return time.monotonic() < self._paused_until

    def _pause_for(self, seconds: float) -> None:
        self._paused_until = max(self._paused_until, time.monotonic() + seconds)

    @property
    def stopping(self) -> bool:
        return self._stop is not None and self._stop.is_set()

    async def _nap(self, seconds: float) -> None:
        """Пауза перед повтором; остановка процесса её прерывает."""
        stop = self._stop
        if stop is None:
            await self._sleep(seconds)
            return
        if stop.is_set():
            return
        nap = asyncio.ensure_future(self._sleep(seconds))
        waiter = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({nap, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (nap, waiter):
                t.cancel()

    # ----------------------------------------------------------------- проход

    async def run_once(self) -> list[NotifyOutcome]:
        """Один проход: захват и отправка (параллельно). На паузе — ничего."""
        if self.paused:
            return []
        cfg = self.settings
        claims = await nw.claim(
            self.sm, lease=cfg.lease, max_attempts=cfg.max_attempts, limit=cfg.batch
        )
        return list(await asyncio.gather(*(self._guard(c) for c in claims)))

    async def _guard(self, c: NotifyClaim) -> NotifyOutcome:
        """Сбой одного уведомления не останавливает проход: аренда истечёт, будет повтор."""
        started = time.monotonic()
        try:
            result = await self.handle(c, started=started)
        except Exception as exc:
            result = NotifyOutcome("error", type(exc).__name__, c.attempts)
        if result.status in _INFO:
            log = logger.info
        elif result.status == "paused":
            log = logger.error
        else:
            log = logger.warning
        log("notify message: status=%s reason=%s attempt=%d", *_fields(result))
        return result

    # ----------------------------------------------------------------- одно уведомление

    async def handle(self, c: NotifyClaim, *, started: float | None = None) -> NotifyOutcome:
        cfg = self.settings
        t0 = time.monotonic() if started is None else started
        send_deadline = t0 + cfg.send_window_s
        try:
            await self._send(c, send_deadline)
        except _Sent as fin:
            return await self._mark(c, fin.status, fin.reason, t0 + cfg.mark_window_s)
        except _Leave as leave:
            if leave.release_after is not None:
                await self._release(c, leave.release_after)
            return NotifyOutcome(leave.status, leave.reason, c.attempts)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _send(self, c: NotifyClaim, deadline: float) -> None:
        """Отправка с повторами в пределах аренды. Исход — исключением: ``_Sent`` (отметить)
        или ``_Leave`` (не отмечать — повтор после истечения аренды). После остановки процесса
        (``stop``) повторы и ожидание паузы не начинаются: начатый запрос дорабатывается."""
        cfg = self.settings
        pause = cfg.retry_pause_s
        last: str | None = None
        for n in range(cfg.max_sends):
            if n and self.stopping:
                raise _Leave("retry", last or "stopping")
            wait = self._paused_until - time.monotonic()
            if wait > 0:
                if time.monotonic() + wait >= deadline:
                    # 429 или ошибка настройки дольше окна: отсрочка до конца паузы без расхода
                    # попытки (иначе через max_attempts пауз уведомление помечалось бы без
                    # отправки, MINOR-1).
                    raise _Leave("retry", last or "paused", release_after=wait)
                await self._nap(wait)
                if self.stopping:
                    raise _Leave("retry", last or "stopping")
            if deadline - time.monotonic() <= 0:
                raise _Leave("retry", last or "deadline")
            last = await self._attempt(c, deadline)
            # Временный сбой: повтор с паузой (429 — пауза уже выставлена).
            if last != "http_429":
                left = deadline - time.monotonic()
                if left <= pause:
                    raise _Leave("retry", last)
                await self._nap(pause)
                pause = min(pause * 2, cfg.retry_max_pause_s)
        raise _Leave("retry", last)

    async def _attempt(self, c: NotifyClaim, deadline: float) -> str:
        """Одна попытка: поиск чата получателя, затем ``sendMessage``. Известный исход —
        исключением (``_Sent``/``_Leave``); временный сбой — его вид (для повтора).

        Срок — отдельно для каждой фазы (MINOR-2): вышел при поиске чата — запрос к Telegram
        ещё не отправлялся, это «не отправлено» (повтор после аренды); вышел посреди
        ``sendMessage`` — запрос мог дойти, отметка без повтора (``unknown``)."""
        lookup = asyncio.timeout(deadline - time.monotonic())
        try:
            async with lookup:
                chat_id = await users.notification_chat_id(self.sm, c.recipient_id)
        except TimeoutError:
            if lookup.expired():
                raise _Leave("retry", "deadline") from None
            return "TimeoutError"  # тайм-аут соединения с БД — повтор
        except Exception as exc:
            if not transient_db_error(exc):  # детерминированная ошибка БД
                raise
            return type(exc).__name__
        if chat_id is None:  # получатель удалён к моменту отправки (Б-47)
            raise _Sent("no_chat")
        left = deadline - time.monotonic()
        if left <= 0:
            raise _Leave("retry", "deadline")
        try:
            async with asyncio.timeout(left):
                await notify(self.api, chat_id, NotificationKind.NEW_MESSAGE, self.webapp_url)
        except TimeoutError:
            # Срок отправки вышел посреди запроса — он мог дойти: не повторять.
            raise _Sent("unknown", "deadline") from None
        except BotApiError as err:
            return self._classify(err)
        raise _Sent("sent")

    def _classify(self, err: BotApiError) -> str:
        """Ошибка Bot API → вид для повтора; постоянные исходы — исключением."""
        cfg = self.settings
        code = err.error_code
        reason = f"http_{code}" if code is not None else "network"
        if code == 429:
            after = float(err.retry_after) if err.retry_after else cfg.default_retry_after_s
            self._pause_for(min(after, cfg.max_retry_after_s))
            return reason
        if code in CONFIG_ERROR_CODES:
            self._pause_for(cfg.pause.total_seconds())
            raise _Leave("paused", reason, release_after=cfg.pause.total_seconds())
        if code in UNREACHABLE_CODES:
            raise _Sent("blocked", reason)
        if err.maybe_sent:
            raise _Sent("unknown", "maybe_sent")
        if err.transient:
            return reason
        raise _Sent("rejected", reason)

    async def _release(self, c: NotifyClaim, after_s: float) -> None:
        """Досрочное снятие аренды без расхода попытки (``pipeline_notify_release``, 0015).
        Сбой БД — не страшно: аренда истечёт сама (попытка тогда засчитается)."""
        try:
            await nw.release(
                self.sm, c, retry_after=timedelta(seconds=after_s), count_attempt=False
            )
        except Exception as exc:
            logger.warning("notify release failed: %s", type(exc).__name__)

    async def _mark(
        self, c: NotifyClaim, status: Status, reason: str | None, deadline: float
    ) -> NotifyOutcome:
        """Отметка по токену; временная ошибка БД — повтор до конца аренды."""
        cfg = self.settings
        pause = cfg.retry_pause_s
        while True:
            try:
                ok = await nw.done(self.sm, c)
                break
            except Exception as exc:
                left = deadline - time.monotonic()
                if not transient_db_error(exc) or left <= 0:
                    raise
                logger.warning("notify mark failed: %s", type(exc).__name__)
                await self._sleep(min(pause, left))
                pause = min(pause * 2, cfg.retry_max_pause_s)
        if not ok:
            return NotifyOutcome("lost", status, c.attempts)
        return NotifyOutcome(status, reason, c.attempts)

    # ----------------------------------------------------------------- процесс

    async def run_forever(self, *, poll_s: float = 1.0, stop: asyncio.Event | None = None) -> None:
        """Непрерывный цикл: до ``batch`` уведомлений одновременно; освободился слот — сразу
        новый захват. Нечего захватывать или пауза — ждём ``poll_s``, конца любой задачи или
        остановки. При остановке — новые не захватываются, повторы не начинаются; начатые
        запросы дорабатываются и отмечаются (дожидаемся их)."""
        stop = stop or asyncio.Event()
        self._stop = stop
        running: set[asyncio.Task[NotifyOutcome]] = set()
        stopper = asyncio.create_task(stop.wait())
        cfg = self.settings
        try:
            while not stop.is_set():
                claims: list[NotifyClaim] = []
                free = cfg.batch - len(running)
                if free > 0 and not self.paused:
                    try:
                        claims = await nw.claim(
                            self.sm, lease=cfg.lease, max_attempts=cfg.max_attempts, limit=free
                        )
                    except Exception as exc:  # БД недоступна и т. п. — повтор после паузы
                        logger.warning("notify worker claim failed: %s", type(exc).__name__)
                for c in claims:
                    task = asyncio.create_task(self._guard(c))
                    running.add(task)
                    task.add_done_callback(running.discard)
                if claims and len(running) < cfg.batch:
                    continue  # очередь, возможно, не пуста — сразу добираем
                await asyncio.wait(
                    {stopper, *running}, timeout=poll_s, return_when=asyncio.FIRST_COMPLETED
                )
        finally:
            stopper.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)


def _fields(o: NotifyOutcome) -> tuple[str, str | None, int]:
    return o.status, o.reason, o.attempts


async def _main() -> None:  # pragma: no cover — запуск процесса (нужны роль входа и токен)
    from app.config import check_notify_config, get_settings
    from app.core.logging import install_log_redaction
    from app.db import make_engine, make_sessionmaker

    install_log_redaction()  # токен бота в URL Bot API — из журналов httpx
    logging.getLogger("httpx").setLevel(logging.WARNING)  # без строки на каждый запрос
    settings = get_settings()
    check_notify_config(settings)
    if not settings.webapp_url:
        logger.warning("FEELINGS_WEBAPP_URL is empty: notifications without Mini App button")
    api = BotApi(settings.telegram_bot_token, max_retries=0)
    worker = NotifyWorker(
        worker_sm=make_sessionmaker(make_engine(settings.worker_database_url)),
        api=api,
        webapp_url=settings.webapp_url,
        settings=NotifySettings(
            lease=timedelta(seconds=settings.notify_lease_s),
            max_attempts=settings.notify_max_attempts,
            batch=settings.notify_batch,
        ),
    )
    stop = asyncio.Event()
    try:
        with stop_on_signals(stop, name="notify worker"):
            await worker.run_forever(poll_s=settings.notify_poll_s, stop=stop)
    finally:
        await api.aclose()
    logger.info("notify worker stopped")


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())

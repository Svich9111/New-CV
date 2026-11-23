"""Фоновый воркер конвейера входящего (ТЗ §6.2 п.3–7, ФТ-09; задача 3.2; АР-27/28, АР-44/45).

Цикл одного сообщения (транзакции — АР-44, Б-45; БД — ``repositories.pipeline_work``):

1. **Захват** (``pipeline_claim``, короткая транзакция): ``processing`` со свободной или
   истёкшей арендой — догон после перезапуска; ``SKIP LOCKED`` — две копии воркера не берут одно
   сообщение. Пара распалась / получателя нет — ``couple_ended``, попытки исчерпаны или сообщение
   ждёт дольше 24 ч от отправки — ``attempts_exhausted``: это завершает сама БД, без модели.
2. **Чтение** (отдельная транзакция, ``pipeline_read``): строка есть, только если аренда своя и
   черновик в статусе ``sent`` (автор нажал «Отправить»). Пусто — ``approve()`` не вызывается,
   сообщение не обрабатывается (Б-48). Расшифровка — с контекстом черновика (Б-39).
3. **Одобрение** — ``pipeline.approve(confirmed=True)`` только после проверки статуса в БД:
   объект одобрения не переживает процесс, «одно «Отправить» — одна доставка» гарантирует БД.
4. **Конвейер** — ``pipeline.process`` вне транзакции (безопасность → тон → адаптация →
   смысл). Вход — только одобренный оригинал: ни памяти, ни диалога, ни имён автора (§6.2,
   §9.2); текст — значение поля JSON, решение о доставке и пометке принимает код (§9.2 п.3).
   Набор промптов — **обязательная настройка** ``FEELINGS_PIPELINE_PROMPT_SET`` без значения по
   умолчанию (АР-26, АР-45): ``v0`` — прототип без прогона, допустим только на стенде
   (``staging``) и в разработке; в ``production`` — только набор из ``PASSED_PROMPT_SETS``
   (прошедший прогон эталонного набора; пока пусто — production не стартует). Подсказка
   получателю (``reply_hint``) не сохраняется и не показывается до прогона ``V2`` (Б-48).
5. **Исход** — одна транзакция на сообщение:

   - доставлено/адаптировано/оригинал с предупреждением → ``pipeline_deliver_leased`` (версия для
     получателя шифруется с контекстом ``partner_messages|adapted_enc|<id>``; события — типом);
   - кризис (АР-28) → ``pipeline_withhold_leased('safety', события, тип кризиса)`` — тип нужен
     экрану 3.4 для протокола §9.3 (шаблон отправителя при ``threat_outgoing``, АР-45 п.3);
   - отказ модели (АР-20, АР-27) → ``model_refusal``: одобрение израсходовано, автоповтора нет;
   - ``bad_request`` (ошибка в коде), шифротекст не открылся, текст не проходит ``approve`` →
     ``pipeline_error``;
   - ``quota``/``auth``/``config``/``model_not_found`` — «сервис не настроен», не свойство
     сообщения (АР-45 п.2): **выключатель** — воркер встаёт на паузу (не захватывает новых), ERROR
     в журнал без текстов; аренда снимается ``pipeline_release(…, пауза, false)`` — попытка не
     расходуется (предел ожидания 24 ч ставит БД);
   - недоступность, испорченный ответ на безопасности/тоне, превышение времени, прочая ошибка
     конвейера → ``pipeline_release(…, 5 с · 2^попытка, true)`` — повтор через короткую задержку
     (бюджет §10), предел попыток ставит БД (``attempts_exhausted``).

   Запись исхода при сбое соединения/записи повторяется с тем же токеном (до ``true``/``false``,
   ограниченно, с паузой; запись по токену идемпотентна, 0014): иначе отказ модели после
   истечения аренды превратился бы в доставку (тест 3.2 D1). Такой проход — ``error`` с
   ``recorded`` = что в итоге записано.

   ``lost``/``false``/``couple_ended`` — штатные исходы (аренду перехватили, аккаунт автора
   удалён, пара распалась или получатель ушёл — Б-44): без повтора и без текста в журнале.

Цикл процесса (``run_forever``) — непрерывный: до ``batch`` сообщений одновременно, свободный слот
сразу занимается новым захватом (одно медленное сообщение не задерживает остальные, §10).

Журнал — только исход, причина, вид ошибки и номер попытки: ни текстов, ни id (ТЗ §9.1 п.5).
Уведомление получателю (3.5) — не здесь: доставленное попадает в очередь ``pipeline_claim_notify``
(Б-47).

Запуск: ``python -m app.workers.pipeline``; БД — ``FEELINGS_WORKER_DATABASE_URL`` (роль входа —
член ``feelings_worker``, Б-69); ключи шифрования — ``FEELINGS_ENCRYPTION_KEYS`` (Б-42)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import pipeline
from app.ai.llm import LLMError, LLMRefusal
from app.ai.models import AgentModels
from app.ai.prompts import V0, V1, V2, PromptSet
from app.config import ConfigError
from app.core.crypto import DecryptionError, Keyring
from app.repositories import pipeline_work as pw
from app.repositories.pipeline_work import PipelineClaim, WithholdReason

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Виды ошибок модели, после которых сообщение повторяется через короткую задержку (одобрение не
# израсходовано, Б-48). На адаптации и смысле они уже дают «оригинал с предупреждением» (АР-27) —
# сюда доходят только с шагов безопасности и тона.
RETRY_KINDS = frozenset({"unavailable", "bad_output"})
# «Сервис не настроен» (АР-45 п.2): лечит оператор, а не повтор через секунды — пауза воркера.
PAUSE_KINDS = frozenset({"quota", "auth", "config", "model_not_found"})

# Наборы промптов по имени настройки. Прошедшие прогон эталонного набора без регрессий (АР-26) —
# только они допустимы в production; пока таких нет.
PROMPT_SETS: dict[str, PromptSet] = {"v0": V0, "v1": V1, "v2": V2}
PASSED_PROMPT_SETS: frozenset[str] = frozenset()
PROTOTYPE_PROMPT_SET = "v0"
PROTOTYPE_ENVS = frozenset({"staging", "dev", "local", "test"})

Status = Literal[
    "delivered",  # доставлено получателю
    "withheld",  # остановлено воркером (reason: safety | model_refusal | pipeline_error)
    "couple_ended",  # пара распалась / получатель удалил аккаунт, пока шла модель
    "lost",  # аренду перехватили, сообщение удалено с аккаунтом автора, черновик не «sent»
    "retry",  # недоступность и т. п. — аренда снята, повтор через короткую задержку
    "paused",  # ошибка настройки модели — воркер на паузе, аренда снята без расхода попытки
    "error",  # сбой БД: исход записан повтором (recorded) или повтор после аренды (recorded=None)
]


def resolve_prompt_set(name: str, env: str) -> PromptSet:
    """Набор промптов воркера из настройки (АР-26, АР-45). Без значения, неизвестный, черновой
    (без прогона) или прототип вне стенда/разработки — ``ConfigError``: воркер не стартует."""
    key = name.strip().lower()
    if not key:
        raise ConfigError(
            "FEELINGS_PIPELINE_PROMPT_SET не задан (АР-45): значения по умолчанию нет"
        )
    if key not in PROMPT_SETS:
        raise ConfigError(
            f"FEELINGS_PIPELINE_PROMPT_SET: неизвестный набор (есть {sorted(PROMPT_SETS)})"
        )
    if key in PASSED_PROMPT_SETS:
        return PROMPT_SETS[key]
    if env == "production":
        raise ConfigError(
            "production: только набор промптов, прошедший прогон эталонного набора (АР-26)"
        )
    if key != PROTOTYPE_PROMPT_SET:
        raise ConfigError("черновой набор промптов без прогона эталонного набора (АР-26)")
    if env not in PROTOTYPE_ENVS:
        raise ConfigError("прототип v0 допустим только на тестовом стенде (staging) и в разработке")
    return PROMPT_SETS[key]


@dataclass(frozen=True)
class PipelineSettings:
    lease: timedelta = timedelta(minutes=3)
    max_attempts: int = 5
    batch: int = 5
    # Конвейер одного сообщения не переживает свою аренду (иначе её перехватит другая копия);
    # ``None`` — аренда минус запас.
    process_timeout_s: float | None = None
    # Повтор после недоступности: ``retry_base · 2^попытка``, не больше ``retry_cap`` (MINOR-1).
    retry_base: timedelta = timedelta(seconds=5)
    retry_cap: timedelta = timedelta(hours=1)
    # Пауза воркера (выключатель) при ошибке настройки модели (АР-45 п.2).
    pause: timedelta = timedelta(minutes=5)
    # Повтор записи исхода при сбое соединения/записи (D1): число повторов и пауза (растёт).
    write_retries: int = 5
    write_retry_pause_s: float = 0.2

    @property
    def timeout_s(self) -> float:
        if self.process_timeout_s is not None:
            return self.process_timeout_s
        lease_s = self.lease.total_seconds()
        return max(lease_s - 30.0, lease_s / 2)

    def retry_delay(self, attempts: int) -> timedelta:
        delay: timedelta = self.retry_base * (2 ** max(attempts, 0))
        return min(delay, self.retry_cap)


@dataclass(frozen=True)
class PipelineOutcome:
    status: Status
    reason: str | None = None  # причина остановки или вид ошибки — без текста
    attempts: int = 0
    adapted: bool = False
    recorded: str | None = None  # при ``error``: исход, записанный повтором записи


class PipelineWorker:
    """``worker_sm`` — подключение, которое может ``SET ROLE feelings_worker``. Набор промптов
    передаётся явно (значения по умолчанию нет, АР-45)."""

    def __init__(
        self,
        *,
        worker_sm: async_sessionmaker[AsyncSession],
        keys: Keyring,
        models: AgentModels,
        prompt_set: PromptSet,
        settings: PipelineSettings | None = None,
    ) -> None:
        self.sm = worker_sm
        self.keys = keys
        self.models = models
        self.prompt_set = prompt_set
        self.settings = settings or PipelineSettings()
        self._paused_until = 0.0  # time.monotonic()

    # ----------------------------------------------------------------- выключатель (АР-45 п.2)

    @property
    def paused(self) -> bool:
        return time.monotonic() < self._paused_until

    def _pause(self, kind: str) -> None:
        pause_s = self.settings.pause.total_seconds()
        if not self.paused:
            logger.error("pipeline paused: model %s, resume in %ds", kind, int(pause_s))
        self._paused_until = max(self._paused_until, time.monotonic() + pause_s)

    # ----------------------------------------------------------------- проход

    async def run_once(self) -> list[PipelineOutcome]:
        """Один проход: захват и обработка сообщений (параллельно). На паузе — ничего."""
        if self.paused:
            return []
        cfg = self.settings
        claims = await pw.claim(
            self.sm, lease=cfg.lease, max_attempts=cfg.max_attempts, limit=cfg.batch
        )
        return list(await asyncio.gather(*(self._guard(c) for c in claims)))

    async def _guard(self, c: PipelineClaim) -> PipelineOutcome:
        """Сбой одного сообщения не останавливает проход: аренда истечёт, сообщение повторится."""
        try:
            result = await self.handle(c)
        except Exception as exc:
            result = PipelineOutcome("error", type(exc).__name__, c.attempts)
        log = logger.warning if result.status in ("retry", "error", "paused") else logger.info
        log(
            "pipeline message: status=%s reason=%s attempt=%d adapted=%s recorded=%s",
            result.status,
            result.reason,
            result.attempts,
            result.adapted,
            result.recorded,
        )
        return result

    # ----------------------------------------------------------------- запись исхода (D1)

    async def _write(self, op: Callable[[], Awaitable[T]]) -> tuple[T, str | None]:
        """Запись исхода по токену с повтором при сбое соединения/записи (та же запись, тот же
        токен — идемпотентно, 0014). → (результат, вид первого сбоя или ``None``). Повторы
        исчерпаны — исключение (повтор после аренды; для отказа аренда не снимается)."""
        cfg = self.settings
        failed: str | None = None
        for n in range(cfg.write_retries + 1):
            try:
                return await op(), failed
            except Exception as exc:
                failed = failed or type(exc).__name__
                logger.warning("pipeline finish write failed: %s", type(exc).__name__)
                if n == cfg.write_retries:
                    raise
                await asyncio.sleep(cfg.write_retry_pause_s * (n + 1))
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _done(
        c: PipelineClaim,
        status: Status,
        failed: str | None,
        reason: str | None = None,
        adapted: bool = False,
    ) -> PipelineOutcome:
        if failed is None:
            return PipelineOutcome(status, reason, c.attempts, adapted)
        return PipelineOutcome("error", failed, c.attempts, recorded=status)

    async def _withhold(
        self,
        c: PipelineClaim,
        reason: WithholdReason,
        events: tuple[str, ...] = (),
        crisis: str | None = None,
    ) -> PipelineOutcome:
        ok, failed = await self._write(lambda: pw.withhold(self.sm, c, reason, events, crisis))
        return self._done(c, "withheld" if ok else "lost", failed, reason=reason)

    async def _release(
        self,
        c: PipelineClaim,
        status: Literal["retry", "paused"],
        reason: str,
        *,
        after: timedelta,
        count_attempt: bool,
    ) -> PipelineOutcome:
        ok, failed = await self._write(
            lambda: pw.release(self.sm, c, retry_after=after, count_attempt=count_attempt)
        )
        return self._done(c, status if ok else "lost", failed, reason=reason)

    async def _retry(self, c: PipelineClaim, reason: str) -> PipelineOutcome:
        delay = self.settings.retry_delay(c.attempts)
        return await self._release(c, "retry", reason, after=delay, count_attempt=True)

    # ----------------------------------------------------------------- одно сообщение

    async def handle(self, c: PipelineClaim) -> PipelineOutcome:
        # 2. Одобренный оригинал по своей аренде; нет — approve() не вызывается (Б-48).
        try:
            original = await pw.read_original(self.sm, self.keys, c)
        except DecryptionError:  # шифротекст не открылся (ключ отозван, контекст) — неустранимо
            return await self._withhold(c, "pipeline_error")
        if original is None:
            return PipelineOutcome("lost", "not_approved", c.attempts)
        # 3. Одобрение — после проверки статуса в БД.
        try:
            approved = pipeline.approve(original, confirmed=True)
        except (ValueError, TypeError):  # пустой или слишком длинный текст — неустранимо
            return await self._withhold(c, "pipeline_error")
        del original
        # 4. Конвейер вне транзакции.
        try:
            delivery = await asyncio.wait_for(
                pipeline.process(self.models, approved, prompt_set=self.prompt_set),
                timeout=self.settings.timeout_s,
            )
        except LLMRefusal:
            # Отказ модели — решение «не доставлять» (АР-20/27): одобрение израсходовано; аренда
            # не снимается досрочно — до записи исхода сообщение никому не выдаётся (D1).
            return await self._withhold(c, "model_refusal")
        except LLMError as exc:
            if exc.kind in RETRY_KINDS:
                return await self._retry(c, exc.kind)
            if exc.kind in PAUSE_KINDS:
                self._pause(exc.kind)
                return await self._release(
                    c, "paused", exc.kind, after=self.settings.pause, count_attempt=False
                )
            return await self._withhold(c, "pipeline_error")  # bad_request: ошибка в коде
        except TimeoutError:
            return await self._retry(c, "timeout")
        except Exception as exc:  # прочий сбой конвейера — повтор, предел попыток ставит БД
            return await self._retry(c, type(exc).__name__)
        # 5. Исход — одна транзакция на сообщение.
        if not delivery.delivered:
            return await self._withhold(
                c,
                "safety",
                delivery.safety_events,
                pw.crisis_type(delivery.safety_event, delivery.safety_events),
            )
        if delivery.shown is None or delivery.tone is None:  # не бывает у доставленного
            return await self._withhold(c, "pipeline_error")
        shown, tone = delivery.shown, delivery.tone
        result, failed = await self._write(
            lambda: pw.deliver(
                self.sm,
                self.keys,
                c,
                shown,
                is_adapted=delivery.is_adapted,
                tone=tone,
                harsh=delivery.harsh_tone_warning,
                events=delivery.safety_events,
            )
        )
        return self._done(c, result, failed, reason=delivery.degraded, adapted=delivery.is_adapted)

    # ----------------------------------------------------------------- процесс

    async def run_forever(self, *, poll_s: float = 1.0, stop: asyncio.Event | None = None) -> None:
        """Непрерывный цикл (MINOR-2): до ``batch`` сообщений одновременно; освободился слот —
        сразу новый захват. Нечего захватывать или пауза — ждём ``poll_s``, конца любой задачи
        или остановки. При остановке — дожидаемся начатых сообщений."""
        stop = stop or asyncio.Event()
        running: set[asyncio.Task[PipelineOutcome]] = set()
        stopper = asyncio.create_task(stop.wait())
        cfg = self.settings
        try:
            while not stop.is_set():
                claims: list[PipelineClaim] = []
                free = cfg.batch - len(running)
                if free > 0 and not self.paused:
                    try:
                        claims = await pw.claim(
                            self.sm, lease=cfg.lease, max_attempts=cfg.max_attempts, limit=free
                        )
                    except Exception as exc:  # БД недоступна и т. п. — повтор после паузы
                        logger.warning("pipeline worker claim failed: %s", type(exc).__name__)
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


async def _main() -> None:  # pragma: no cover — запуск процесса (нужны роль входа и ключи)
    from app.ai.models import openrouter_models_async
    from app.config import get_settings
    from app.core.crypto import load_keyring
    from app.db import make_engine, make_sessionmaker

    settings = get_settings()
    if not settings.worker_database_url:
        raise ConfigError("FEELINGS_WORKER_DATABASE_URL не задан (роль входа воркера, Б-69)")
    prompt_set = resolve_prompt_set(settings.pipeline_prompt_set, settings.env)
    worker = PipelineWorker(
        worker_sm=make_sessionmaker(make_engine(settings.worker_database_url)),
        keys=load_keyring(settings),  # Б-42: ключи шифрования воркера — из окружения
        models=await openrouter_models_async(
            settings.llm_writer_model, settings.llm_classifier_model
        ),
        prompt_set=prompt_set,
        settings=PipelineSettings(
            lease=timedelta(seconds=settings.pipeline_lease_s),
            max_attempts=settings.pipeline_max_attempts,
            batch=settings.pipeline_batch,
            pause=timedelta(seconds=settings.pipeline_pause_s),
        ),
    )
    await worker.run_forever(poll_s=settings.pipeline_poll_s)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())

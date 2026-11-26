"""Фоновый воркер долговременной памяти (ТЗ §6.3, задача 2.4; АР-36, Б-69).

Цикл: захват работы (короткая транзакция под ``feelings_worker``: только id, отметки, аренда) →
модель вне транзакций → запись одной транзакцией владельца (``feelings_app``, RLS) с завершением
последним (``app.ai.memory``). Ответ в чате воркер не блокирует: это отдельный процесс.

- **Догон после перезапуска**: отметки и аренда — в БД; задача, чью аренду не завершили (процесс
  упал), снова захватывается по истечении аренды; ``memory_attempts`` > ``max_attempts`` —
  завершение без извлечения/сжатия (сессия не зацикливает воркер).
- **Две копии**: захват ``FOR NO KEY UPDATE SKIP LOCKED`` и токен аренды; завершение с чужим или
  перехваченным токеном — ``false`` → откат записи.
- **Удаление аккаунта во время работы** — ``gone`` (42501/23503), без повтора (Б-44).
- Журнал — только статус, счётчики и вид ошибки (без текстов, без id пользователя).
- **Пульс** (Б-85, ``app.workers.health``): после каждого успешного прохода (захват прошёл) и во
  время работы над задачей — пока она в пределах своей аренды; БД недоступна или задача зависла —
  пульс устаревает, health check — unhealthy. БД отвергла роль входа — ``ConfigError``, выход с
  кодом 78; модель недоступна при старте — повтор в процессе (``app.workers.startup``).
- **Ключ модели отклонён во время работы** (исход задачи ``error`` с видом
  ``auth``/``quota``/``config``/``model_not_found`` — «сервис не настроен», как выключатель
  конвейера АР-45 п.2; A8 р.1 MINOR-2): пауза воркера на ``pause`` — новое не захватывается,
  остаток пачки не обрабатывается (вызовов модели нет), пульс ``paused`` (unhealthy). После паузы
  — **пробный захват одной задачи**; ``paused`` держится, пока модель не ответит. Попытки:
  функции снятия аренды у памяти нет (захват сам прибавляет попытку), поэтому за паузу расходуется
  не больше одной попытки у задач, захваченных до отказа, и по одной у пробной; успех обнуляет
  счётчик. Пока ключ отклонён, очередь попытки не расходует.
- **Остановка** (SIGTERM; ревью р.1 MINOR-1): ``stop`` проверяется перед каждым захватом и
  между задачами пачки — новое (в том числе сжатие) не захватывается, начатая задача
  дорабатывается; оставшиеся захваченные — до истечения аренды (у memory нет
  ``stop_grace_period``: Docker ждёт 10 с).

Запуск: ``python -m app.workers.memory``; БД — ``FEELINGS_WORKER_DATABASE_URL`` (роль входа —
член ``feelings_worker`` и ``feelings_app``, NOINHERIT; Б-69 — инфраструктура)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import memory
from app.ai.embeddings import Embedder
from app.ai.models import AgentModels
from app.config import Settings
from app.core.crypto import Keyring
from app.core.fingerprint import FingerprintKeys
from app.repositories import memory_work as mw
from app.workers.health import Heartbeat
from app.workers.pipeline import PAUSE_KINDS
from app.workers.startup import raise_if_config_db_error, run_worker, startup_retry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerSettings:
    session_gap: timedelta = timedelta(minutes=30)
    lease: timedelta = timedelta(minutes=10)
    compact_every: timedelta = timedelta(days=7)
    max_attempts: int = 5
    batch: int = 10
    merge_distance: float = memory.MERGE_DISTANCE
    # Пауза при ошибке настройки модели (auth/quota/config/model_not_found).
    pause: timedelta = timedelta(minutes=5)


def _model_answered(result: memory.Outcome) -> bool:
    """Модель ответила хотя бы на один вызов задачи (ключ принят) — пауза снимается."""
    return result.status == "refused" or any(c.error is None for c in result.calls)


class MemoryWorker:
    """``worker_sm`` — подключение, которое может ``SET ROLE feelings_worker``; ``app_sm`` — то,
    что может ``SET ROLE feelings_app`` (``user_scope``). В продуктиве — одна роль входа (Б-69)."""

    def __init__(
        self,
        *,
        worker_sm: async_sessionmaker[AsyncSession],
        app_sm: async_sessionmaker[AsyncSession],
        keys: Keyring,
        fingerprints: FingerprintKeys,
        models: AgentModels,
        embedder: Embedder | None = None,
        settings: WorkerSettings | None = None,
        heartbeat: Heartbeat | None = None,
    ) -> None:
        self.heartbeat = heartbeat or Heartbeat(None)
        self.worker_sm = worker_sm
        self.app_sm = app_sm
        self.keys = keys
        self.fingerprints = fingerprints
        self.models = models
        self.embedder = embedder
        self.settings = settings or WorkerSettings()
        self._paused_until = 0.0  # time.monotonic()
        # Вид ошибки настройки модели; держится до первого ответа модели (пульс ``paused``).
        self._pause_kind: str | None = None

    # ----------------------------------------------------------------- пауза (настройка модели)

    @property
    def paused(self) -> bool:
        return time.monotonic() < self._paused_until

    def _pulse(self) -> None:
        if self._pause_kind is not None:
            self.heartbeat.beat("paused", self._pause_kind)
        else:
            self.heartbeat.beat("ok")

    def _limit(self) -> int:
        """После паузы — пробный захват одной задачи, пока модель не ответит."""
        return 1 if self._pause_kind is not None else self.settings.batch

    def _blocked(self, stop: asyncio.Event | None) -> bool:
        return self.paused or (stop is not None and stop.is_set())

    def _note(self, result: memory.Outcome) -> None:
        if result.status == "error" and result.error in PAUSE_KINDS:
            pause_s = self.settings.pause.total_seconds()
            if not self.paused:
                logger.error("memory paused: model %s, resume in %ds", result.error, int(pause_s))
            self._paused_until = time.monotonic() + pause_s
            self._pause_kind = result.error
            self._pulse()
        elif self._pause_kind is not None and _model_answered(result):
            logger.info("memory resumed: model answered")
            self._pause_kind = None
            self._pulse()

    # ----------------------------------------------------------------- проход

    async def run_once(self, stop: asyncio.Event | None = None) -> list[memory.Outcome]:
        """Один проход: захват и обработка извлечения, затем сжатия. На паузе (ошибка
        настройки модели) или после остановки — ничего нового не захватывается."""
        cfg = self.settings
        out: list[memory.Outcome] = []
        if self.paused:  # цикл жив, захват не нужен
            self._pulse()
        if self._blocked(stop):
            return out
        extractions = await mw.claim_extraction(
            self.worker_sm, gap=cfg.session_gap, lease=cfg.lease, limit=self._limit()
        )
        self._pulse()
        for n, claim in enumerate(extractions):
            if self._blocked(stop):
                self._left(len(extractions) - n)
                return out
            out.append(
                await self._guard(
                    "extract",
                    memory.run_extraction(
                        self.app_sm,
                        self.keys,
                        self.fingerprints,
                        self.models,
                        claim,
                        embedder=self.embedder,
                        max_attempts=cfg.max_attempts,
                        merge_distance=cfg.merge_distance,
                    ),
                )
            )
            self._note(out[-1])
        if self._blocked(stop):
            return out
        compactions = await mw.claim_compaction(
            self.worker_sm, every=cfg.compact_every, lease=cfg.lease, limit=self._limit()
        )
        self._pulse()
        for n, comp in enumerate(compactions):
            if self._blocked(stop):
                self._left(len(compactions) - n)
                return out
            out.append(
                await self._guard(
                    "compact",
                    memory.run_compaction(
                        self.app_sm,
                        self.keys,
                        self.fingerprints,
                        self.models,
                        comp,
                        embedder=self.embedder,
                        max_attempts=cfg.max_attempts,
                        merge_distance=cfg.merge_distance,
                    ),
                )
            )
            self._note(out[-1])
        return out

    @staticmethod
    def _left(n: int) -> None:
        """Захваченные, но не начатые задачи (остановка или пауза): аренда истечёт сама."""
        logger.warning("memory: %d claimed jobs left until lease expiry", n)

    async def _guard(
        self, kind: Literal["extract", "compact"], job: Awaitable[memory.Outcome]
    ) -> memory.Outcome:
        """Сбой одной задачи не останавливает проход: аренда истечёт, задача повторится.
        Пульс идёт, пока задача в пределах своей аренды (Б-85)."""
        try:
            async with self.heartbeat.working(self.settings.lease.total_seconds()):
                result = await job
        except Exception as exc:
            logger.warning("memory %s failed: %s", kind, type(exc).__name__)
            return memory.Outcome(kind, "error", error="internal")
        if result.status == "skipped":
            # Задача исчерпала попытки и завершена без извлечения/сжатия — потеря памяти (MINOR-3).
            logger.error("memory %s skipped after max attempts", result.kind)
        logger.info(
            "memory %s: status=%s inserted=%d merged=%d repeated=%d forgotten=%d dropped=%d "
            "error=%s",
            result.kind,
            result.status,
            result.inserted,
            result.merged,
            result.repeated,
            result.forgotten,
            result.dropped,
            result.error,
        )
        return result

    async def run_forever(self, *, poll_s: float = 30.0, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                done = await self.run_once(stop)
            except Exception as exc:  # БД недоступна и т. п. — повтор после паузы
                raise_if_config_db_error(exc, name="memory worker")
                logger.warning("memory worker pass failed: %s", type(exc).__name__)
                done = []
            if not done or self.paused:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll_s)
                except TimeoutError:
                    pass


async def _main() -> None:  # pragma: no cover — запуск процесса (нужны роль входа и ключи)
    from app.ai import models as ai_models
    from app.config import ConfigError, get_settings
    from app.core.crypto import load_keyring
    from app.core.fingerprint import load_fingerprint_keys
    from app.workers.signals import stop_on_signals

    heartbeat = Heartbeat.from_env()
    heartbeat.beat("starting")
    settings = get_settings()
    if not settings.worker_database_url:
        raise ConfigError("FEELINGS_WORKER_DATABASE_URL не задан (роль входа воркера, Б-69)")
    keys = load_keyring(settings)
    fingerprints = load_fingerprint_keys(settings)
    stop = asyncio.Event()
    with stop_on_signals(stop, name="memory worker"):
        # Каталог цен OpenRouter недоступен при старте — повтор в процессе (Б-90).
        models = await startup_retry(
            lambda: ai_models.openrouter_models_async(
                settings.llm_writer_model, settings.llm_classifier_model
            ),
            name="memory worker",
            what="models",
            heartbeat=heartbeat,
            stop=stop,
        )
        if models is not None:  # ``None`` — остановка во время ожидания
            await _loop(settings, models, keys, fingerprints, heartbeat, stop)
    logger.info("memory worker stopped")


async def _loop(  # pragma: no cover — запуск процесса
    settings: Settings,
    models: AgentModels,
    keys: Keyring,
    fingerprints: FingerprintKeys,
    heartbeat: Heartbeat,
    stop: asyncio.Event,
) -> None:
    from app.db import make_engine, make_sessionmaker

    sm = make_sessionmaker(make_engine(settings.worker_database_url))
    worker = MemoryWorker(
        worker_sm=sm,
        app_sm=sm,
        keys=keys,
        fingerprints=fingerprints,
        models=models,
        embedder=None,  # модель эмбеддингов не выбрана (Б-66): память по свежести
        settings=WorkerSettings(
            session_gap=timedelta(minutes=settings.memory_session_gap_min),
            lease=timedelta(minutes=settings.memory_lease_min),
            compact_every=timedelta(hours=settings.memory_compact_every_h),
            max_attempts=settings.memory_max_attempts,
        ),
        heartbeat=heartbeat,
    )
    await worker.run_forever(stop=stop)


if __name__ == "__main__":  # pragma: no cover
    run_worker(_main, name="memory worker")  # ошибка настройки — код выхода 78

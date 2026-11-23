"""Фоновый воркер конвейера входящего (ТЗ §6.2 п.3–7, ФТ-09; задача 3.2; АР-27, АР-28, АР-44).

Цикл одного сообщения (транзакции — АР-44, Б-45; БД — ``repositories.pipeline_work``):

1. **Захват** (``pipeline_claim``, короткая транзакция): ``processing`` со свободной или
   истёкшей арендой — догон после перезапуска; ``SKIP LOCKED`` — две копии воркера не берут одно
   сообщение. Пара распалась / получателя нет — ``couple_ended``, попытки исчерпаны —
   ``attempts_exhausted``: это завершает сама БД, без модели.
2. **Чтение** (отдельная транзакция, ``pipeline_read``): строка есть, только если аренда своя и
   черновик в статусе ``sent`` (автор нажал «Отправить»). Пусто — ``approve()`` не вызывается,
   сообщение не обрабатывается (Б-48). Расшифровка — с контекстом черновика (Б-39).
3. **Одобрение** — ``pipeline.approve(confirmed=True)`` только после проверки статуса в БД:
   объект одобрения не переживает процесс, «одно «Отправить» — одна доставка» гарантирует БД.
4. **Конвейер** — ``pipeline.process`` вне транзакции (безопасность → тон → адаптация →
   смысл). Вход — только одобренный оригинал: ни памяти, ни диалога, ни имён автора (§6.2,
   §9.2); текст — значение поля JSON, решение о доставке и пометке принимает код (§9.2 п.3).
   Набор промптов — ``V0`` по умолчанию (как задано в 3.2; см. АР-26). Подсказка получателю
   (``reply_hint``) не сохраняется и не показывается до прогона ``V2`` (Б-48).
5. **Завершение** — одна транзакция на сообщение:

   - доставлено/адаптировано/оригинал с предупреждением → ``pipeline_deliver_leased`` (версия для
     получателя шифруется с контекстом ``partner_messages|adapted_enc|<id>``; события — типом);
   - кризис (АР-28) → ``pipeline_withhold_leased('safety')`` с событиями от имени автора; автор
     видит в ленте ``withheld``/``safety`` («не отправлено» и помощь — экран 3.4);
   - отказ модели (АР-20, АР-27) → ``model_refusal``: одобрение израсходовано, автоповтора нет;
   - неустранимая ошибка (настройка модели, ключ, лимит, запрос; шифротекст не открылся; текст
     не проходит ``approve``) → ``pipeline_error``;
   - недоступность модели, испорченный ответ на шагах безопасности/тона, превышение времени,
     сбой БД или прочая ошибка → **ничего не вызывается**: повтор после истечения аренды, предел
     попыток ставит БД (``attempts_exhausted``).

   ``lost``/``false``/``couple_ended`` — штатные исходы (аренду перехватили, аккаунт автора
   удалён, пара распалась или получатель ушёл — Б-44): без повтора и без текста в журнале.

Журнал — только исход, причина, вид ошибки и номер попытки: ни текстов, ни id (ТЗ §9.1 п.5).
Уведомление получателю (3.5) — не здесь: доставленное попадает в очередь ``pipeline_claim_notify``
(Б-47).

Запуск: ``python -m app.workers.pipeline``; БД — ``FEELINGS_WORKER_DATABASE_URL`` (роль входа —
член ``feelings_worker``, Б-69); ключи шифрования — ``FEELINGS_ENCRYPTION_KEYS`` (Б-42)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import pipeline
from app.ai.llm import LLMError, LLMRefusal
from app.ai.models import AgentModels
from app.ai.prompts import V0, PromptSet
from app.core.crypto import DecryptionError, Keyring
from app.repositories import pipeline_work as pw
from app.repositories.pipeline_work import PipelineClaim, WithholdReason

logger = logging.getLogger(__name__)

# Виды ошибок модели, после которых сообщение повторяется (одобрение не израсходовано, Б-48).
# На адаптации и смысле они уже дают «оригинал с предупреждением» (АР-27) — сюда доходят только
# с шагов безопасности и тона, где без ответа модели доставлять нельзя.
RETRY_KINDS = frozenset({"unavailable", "bad_output"})

Status = Literal[
    "delivered",  # доставлено получателю
    "withheld",  # остановлено воркером (reason: safety | model_refusal | pipeline_error)
    "couple_ended",  # пара распалась / получатель удалил аккаунт, пока шла модель
    "lost",  # аренду перехватили, сообщение удалено с аккаунтом автора, черновик не «sent»
    "retry",  # недоступность — повтор после аренды (ничего не вызвано)
    "error",  # сбой (БД и т. п.) — повтор после аренды
]


@dataclass(frozen=True)
class PipelineSettings:
    lease: timedelta = timedelta(minutes=3)
    max_attempts: int = 5
    batch: int = 5
    # Конвейер одного сообщения не переживает свою аренду (иначе её перехватит другая копия);
    # ``None`` — аренда минус запас.
    process_timeout_s: float | None = None

    @property
    def timeout_s(self) -> float:
        if self.process_timeout_s is not None:
            return self.process_timeout_s
        lease_s = self.lease.total_seconds()
        return max(lease_s - 30.0, lease_s / 2)


@dataclass(frozen=True)
class PipelineOutcome:
    status: Status
    reason: str | None = None  # причина остановки или вид ошибки — без текста
    attempts: int = 0
    adapted: bool = False


class PipelineWorker:
    """``worker_sm`` — подключение, которое может ``SET ROLE feelings_worker``."""

    def __init__(
        self,
        *,
        worker_sm: async_sessionmaker[AsyncSession],
        keys: Keyring,
        models: AgentModels,
        prompt_set: PromptSet = V0,
        settings: PipelineSettings | None = None,
    ) -> None:
        self.sm = worker_sm
        self.keys = keys
        self.models = models
        self.prompt_set = prompt_set
        self.settings = settings or PipelineSettings()

    async def run_once(self) -> list[PipelineOutcome]:
        """Один проход: захват и обработка сообщений (параллельно — бюджет §10)."""
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
        log = logger.warning if result.status in ("retry", "error") else logger.info
        log(
            "pipeline message: status=%s reason=%s attempt=%d adapted=%s",
            result.status,
            result.reason,
            result.attempts,
            result.adapted,
        )
        return result

    async def _withhold(
        self, c: PipelineClaim, reason: WithholdReason, events: tuple[str, ...] = ()
    ) -> PipelineOutcome:
        ok = await pw.withhold(self.sm, c, reason, events)
        return PipelineOutcome("withheld" if ok else "lost", reason, c.attempts)

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
            # Отказ модели — решение «не доставлять» (АР-20/27): одобрение израсходовано.
            return await self._withhold(c, "model_refusal")
        except LLMError as exc:
            if exc.kind in RETRY_KINDS:
                return PipelineOutcome("retry", exc.kind, c.attempts)
            return await self._withhold(c, "pipeline_error")  # настройка, ключ, лимит, запрос
        except TimeoutError:
            return PipelineOutcome("retry", "timeout", c.attempts)
        # 5. Завершение — одна транзакция на сообщение.
        if not delivery.delivered:
            return await self._withhold(c, "safety", delivery.safety_events)
        if delivery.shown is None or delivery.tone is None:  # не бывает у доставленного
            return await self._withhold(c, "pipeline_error")
        result = await pw.deliver(
            self.sm,
            self.keys,
            c,
            delivery.shown,
            is_adapted=delivery.is_adapted,
            tone=delivery.tone,
            harsh=delivery.harsh_tone_warning,
            events=delivery.safety_events,
        )
        return PipelineOutcome(result, delivery.degraded, c.attempts, adapted=delivery.is_adapted)

    async def run_forever(self, *, poll_s: float = 1.0, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                done = await self.run_once()
            except Exception as exc:  # БД недоступна и т. п. — повтор после паузы
                logger.warning("pipeline worker pass failed: %s", type(exc).__name__)
                done = []
            if not done:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll_s)
                except TimeoutError:
                    pass


async def _main() -> None:  # pragma: no cover — запуск процесса (нужны роль входа и ключи)
    from app.ai.models import openrouter_models_async
    from app.config import ConfigError, get_settings
    from app.core.crypto import load_keyring
    from app.db import make_engine, make_sessionmaker

    settings = get_settings()
    if not settings.worker_database_url:
        raise ConfigError("FEELINGS_WORKER_DATABASE_URL не задан (роль входа воркера, Б-69)")
    worker = PipelineWorker(
        worker_sm=make_sessionmaker(make_engine(settings.worker_database_url)),
        keys=load_keyring(settings),  # Б-42: ключи шифрования воркера — из окружения
        models=await openrouter_models_async(
            settings.llm_writer_model, settings.llm_classifier_model
        ),
        settings=PipelineSettings(
            lease=timedelta(seconds=settings.pipeline_lease_s),
            max_attempts=settings.pipeline_max_attempts,
            batch=settings.pipeline_batch,
        ),
    )
    await worker.run_forever(poll_s=settings.pipeline_poll_s)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())

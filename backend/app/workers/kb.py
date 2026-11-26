"""Фоновый воркер индексации базы знаний (ТЗ §6.4, §10; задача 2.6; АР-39).

Цикл: ``kb_claim_index`` (аренда, ``SKIP LOCKED`` — две копии не дублируют работу; брошенный
материал с истёкшей арендой захватывается снова — догон после перезапуска) → исходник → разбор в
отдельном процессе с тайм-аутом и пределом памяти (``app.kb.parse.parse_isolated``) →
нормализация и фрагменты (``app.kb.chunk``) → метки моделью (``app.kb.tag``; сбой — пустые) →
векторы (эмбеддер; нет — без векторов) → одна транзакция: фрагменты, векторы, завершение.
Сбой разбора — ``kb_fail_index(код)``; попыток больше ``max_attempts`` — ``attempts_exhausted``;
``NULL``/``false`` функций — работа брошена (аренду перехватили, материал удалён администратором).
Журнал — id материала, статус, код, число фрагментов; текстов книг нет.

Пульс (Б-85, ``app.workers.health``): после каждого успешного захвата и во время индексации —
пока она в пределах аренды материала; БД недоступна или индексация зависла — пульс устаревает.
БД отвергла роль входа — ``ConfigError``, выход с кодом 78; модель недоступна при старте —
повтор в процессе; ключа OpenRouter нет — индексация без меток, как раньше
(``app.workers.startup``).

Остановка (SIGTERM; ревью р.1 MINOR-1): ``stop`` проверяется перед захватом и между материалами
пачки — новое не захватывается, начатая индексация дорабатывается; захваченные и не начатые — до
истечения аренды (у kb нет ``stop_grace_period``: Docker ждёт 10 с)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import Embedder, EmbeddingError, check_dim, check_model
from app.ai.llm import LLMConfigError, LLMError
from app.ai.models import AgentModels
from app.kb.chunk import ChunkParams, split_chunks
from app.kb.parse import ParseError, ParseLimits, parse, parse_isolated
from app.kb.tag import tag_chunks
from app.repositories import kb_work
from app.repositories.kb_work import ChunkRow, IndexClaim, LeaseLost
from app.workers.health import Heartbeat
from app.workers.startup import TRANSIENT_LLM, raise_if_config_db_error, run_worker, startup_retry

logger = logging.getLogger(__name__)

ATTEMPTS_EXHAUSTED = "attempts_exhausted"
NO_CHUNKS = "empty_text"
TOO_MANY_CHUNKS = "too_many_chunks"
EMBED_BATCH = 64


@dataclass(frozen=True)
class KbSettings:
    lease: timedelta = timedelta(minutes=30)
    batch: int = 2
    max_attempts: int = 3
    max_chunks: int = 5000
    limits: ParseLimits = field(default_factory=ParseLimits)
    chunking: ChunkParams = field(default_factory=ChunkParams)
    isolate: bool = True  # разбор в отдельном процессе (тайм-аут, память); False — только тесты


@dataclass(frozen=True)
class IndexOutcome:
    status: Literal["indexed", "failed", "lost", "error"]
    code: str | None = None
    chunks: int = 0


class KbWorker:
    def __init__(
        self,
        *,
        worker_sm: async_sessionmaker[AsyncSession],
        models: AgentModels | None = None,
        embedder: Embedder | None = None,
        settings: KbSettings | None = None,
        heartbeat: Heartbeat | None = None,
    ) -> None:
        self.heartbeat = heartbeat or Heartbeat(None)
        self.sm = worker_sm
        self.models = models
        self.embedder = embedder
        self.settings = settings or KbSettings()

    async def run_once(self, stop: asyncio.Event | None = None) -> list[IndexOutcome]:
        out: list[IndexOutcome] = []
        if stop is not None and stop.is_set():
            return out
        claims = await kb_work.claim_index(
            self.sm, lease=self.settings.lease, limit=self.settings.batch
        )
        self.heartbeat.beat("ok")
        for n, claim in enumerate(claims):
            if stop is not None and stop.is_set():
                logger.warning("kb: %d claimed documents left until lease expiry", len(claims) - n)
                break
            try:
                async with self.heartbeat.working(self.settings.lease.total_seconds()):
                    result = await self.index(claim)
            except Exception as exc:  # сбой одной задачи не останавливает проход
                logger.warning("kb index %s failed: %s", claim.document_id, type(exc).__name__)
                result = IndexOutcome("error", "internal")
            log = logger.error if result.status == "failed" else logger.info
            log(
                "kb index %s: status=%s code=%s chunks=%d",
                claim.document_id,
                result.status,
                result.code,
                result.chunks,
            )
            out.append(result)
        return out

    async def _fail(self, claim: IndexClaim, code: str) -> IndexOutcome:
        ok = await kb_work.fail(self.sm, claim, code)
        return IndexOutcome("failed" if ok else "lost", code)

    async def _parse(self, data: bytes, fmt: str) -> str:
        limits = self.settings.limits
        if self.settings.isolate:
            return await asyncio.to_thread(parse_isolated, data, fmt, limits)
        return parse(data, fmt, limits)

    async def index(self, claim: IndexClaim) -> IndexOutcome:
        s = self.settings
        if claim.attempts > s.max_attempts:
            return await self._fail(claim, ATTEMPTS_EXHAUSTED)
        try:
            data, approach = await kb_work.read_source(self.sm, claim)
        except LeaseLost:
            return IndexOutcome("lost")
        try:
            text = await self._parse(data, claim.source_format)
        except ParseError as exc:
            return await self._fail(claim, exc.code)
        del data  # исходник в памяти не держим дольше нужного
        chunks = split_chunks(text, s.chunking)
        if not chunks:
            return await self._fail(claim, NO_CHUNKS)
        if len(chunks) > s.max_chunks:
            return await self._fail(claim, TOO_MANY_CHUNKS)
        tags = await tag_chunks(self.models, chunks, approach=approach)
        vectors: list[list[float] | None] = [None] * len(chunks)
        model = None
        if self.embedder is not None:
            try:
                model = check_model(self.embedder.model)
                vectors = []
                for start in range(0, len(chunks), EMBED_BATCH):
                    part = await self.embedder.embed(chunks[start : start + EMBED_BATCH])
                    vectors += [check_dim(v) for v in part]
            except EmbeddingError:
                model, vectors = None, [None] * len(chunks)  # без векторов — поиск не найдёт
        rows = [
            ChunkRow(c, t.topics, t.level, v) for c, t, v in zip(chunks, tags, vectors, strict=True)
        ]
        try:
            await kb_work.write_index(self.sm, claim, rows, model)
        except LeaseLost:
            return IndexOutcome("lost")
        return IndexOutcome("indexed", None, len(rows))

    async def run_forever(self, *, poll_s: float = 30.0, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                done = await self.run_once(stop)
            except Exception as exc:
                raise_if_config_db_error(exc, name="kb worker")
                logger.warning("kb worker pass failed: %s", type(exc).__name__)
                done = []
            if not done:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll_s)
                except TimeoutError:
                    pass


async def _main() -> None:  # pragma: no cover — запуск процесса (нужна роль входа воркера)
    from app.ai import models as ai_models
    from app.config import ConfigError, get_settings
    from app.db import make_engine, make_sessionmaker
    from app.workers.signals import stop_on_signals

    heartbeat = Heartbeat.from_env()
    heartbeat.beat("starting")
    settings = get_settings()
    if not settings.worker_database_url:
        raise ConfigError("FEELINGS_WORKER_DATABASE_URL не задан (роль входа воркера, Б-69)")

    async def load_models() -> AgentModels | None:
        try:
            return await ai_models.openrouter_models_async(
                settings.llm_writer_model, settings.llm_classifier_model
            )
        except LLMConfigError:  # без ключа — метки пустые, индексация идёт
            logger.warning("kb worker: no model key, indexing without tags")
            return None
        except LLMError as exc:  # модель не найдена, ключ отклонён — тоже без меток
            if isinstance(exc, TRANSIENT_LLM):
                raise  # недоступность при старте — повтор (startup_retry)
            logger.warning("kb worker: model %s, indexing without tags", exc.kind)
            return None

    stop = asyncio.Event()
    with stop_on_signals(stop, name="kb worker"):
        models = await startup_retry(
            load_models, name="kb worker", what="models", heartbeat=heartbeat, stop=stop
        )
        if not stop.is_set():  # остановка во время ожидания — сразу выход
            worker = KbWorker(
                worker_sm=make_sessionmaker(make_engine(settings.worker_database_url)),
                models=models,
                embedder=None,  # модель эмбеддингов не выбрана (Б-66)
                heartbeat=heartbeat,
            )
            await worker.run_forever(stop=stop)
    logger.info("kb worker stopped")


if __name__ == "__main__":  # pragma: no cover
    run_worker(_main, name="kb worker")  # ошибка настройки — код выхода 78

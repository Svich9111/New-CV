"""Долговременная память агента владельца (ТЗ §6.3, §2 п.6, ФТ-12, ФТ-14; задача 2.4, этап 1).

**Извлечение** — по завершённой сессии (Б-54, АР-36: реплики подряд, пока пауза не больше
порога): из реплик **владельца** диапазона ``(after, through]`` и из одобренных автором и
**доставленных владельцу** сообщений пары (``from_partner`` со ссылкой на сообщение) модель
(``memory_extract.v0``, черновик без прогона, АР-26) выписывает записи видов §6.3; наблюдения агента
— отдельный вид ``observation`` с источником ``agent_inference`` и уверенностью не выше 0.6.
Каждая запись: текст шифротекстом, отпечаток (АР-36), дата события, уверенность, источник.

**Удалённое не возвращается** (§6.3 п.4):
1. отметка извлечения: реплики до неё не обрабатываются повторно никогда (главная защита: удалённое
   из старых диалогов не извлекается снова);
2. отпечатки удалённых записей (``app_memory_forgotten``) — перед каждой вставкой и слиянием и при
   сжатии; отпечаток ловит совпадение нормализованного текста (регистр, «ё», пунктуация), но не
   перефразировку (АР-36);
3. сводки (АР-37, допущение до решения оператора): удаление записи (``forget_memory``) сразу
   обнуляет сводку профиля и помечает сводки диалогов «на очистку» — помеченная сводка в контекст
   не идёт; фоновая ``run_forget`` с удалённым текстом в памяти процесса переписывает сводку
   (``summary_forget.v0``) и пересобирает профиль из живых записей; не выполнилась — сжатие памяти
   обнуляет помеченную сводку с сохранением отметки. Сравнение предложений сводки с отпечатками
   (прежняя логика 2.4) убрано: сводки пишет модель своими словами, совпадений почти не бывает.
4. из сообщений партнёра — только текущей активной пары (АР-37, ФТ-14); реплики владельца и
   сообщения партнёра извлекаются **разными вызовами**, источник задаёт код (ревью MINOR-2);
   длинная сессия — пачками (MINOR-3).

**Слияние** (§6.3 п.1): совпадение отпечатка с живой записью — повтор (уверенность не ниже новой);
близкая по смыслу запись того же вида и источника (соседи ``app_memory_nearest``, расстояние не
больше ``MERGE_DISTANCE``) — модель ``memory_merge.v0`` решает, одно ли это содержание, и даёт одну
запись; разные источники и наблюдения со словами владельца не сливаются (источник не теряется).

**Недельное сжатие** (§6.3 п.3): дубликаты (отпечаток) и близкие записи объединяются, сводка
профиля (``agents.profile_summary_enc``) пересобирается (``profile_summary.v0``).

Порядок работы (АР-36): захват — воркером (``app.workers.memory``); чтение — транзакцией владельца;
модели — **вне** транзакций; запись — одной транзакцией владельца, завершение
(``app_memory_*_done``) последним: ``false`` — откат, двойной работы нет. Владелец удалён во время
работы — ``gone`` без повтора (Б-44). Журналов с текстами модуль не пишет."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.calls import CallLog, CallRecord, complete, nullable, strict_object, text_field
from app.ai.embeddings import Embedder, EmbeddingError, check_dim, check_model
from app.ai.llm import LLMError, LLMRefusal
from app.ai.models import AgentModels
from app.ai.prompts import load
from app.core.crypto import Keyring
from app.core.fingerprint import FingerprintKeys, normalize
from app.db import user_scope
from app.repositories import memory_work as mw
from app.repositories.chat import HistorySummary, pack_summary, unpack_summary
from app.repositories.memory_work import CompactionClaim, ExtractionClaim, LiveMemory, SourceText

EXTRACT_VERSION = "v0"  # черновики без прогона (АР-26)
MERGE_VERSION = "v0"
PROFILE_VERSION = "v0"
FORGET_VERSION = "v0"
FORGET_RETRIES = 3

OWNER_KINDS = ("fact", "date", "preference", "sensitive_topic")
KINDS = (*OWNER_KINDS, "observation", "from_partner")
SOURCE_OF: dict[str, str] = {"observation": "agent_inference", "from_partner": "partner_message"}
OBSERVATION_MAX_CONFIDENCE = 0.6
MAX_ITEM_CHARS = 300
MAX_ITEMS = 30
MAX_PROFILE_CHARS = 1000
MERGE_DISTANCE = 0.15  # косинусное расстояние: «близко по смыслу»
NEIGHBOURS = 3
MAX_TOKENS = 2000
BATCH_ITEMS = 40  # реплик или сообщений в одном вызове извлечения
BATCH_CHARS = 8_000

Status = Literal["done", "empty", "skipped", "refused", "error", "lost", "gone"]


@dataclass(frozen=True)
class Candidate:
    kind: str
    source: str
    text: str
    confidence: float
    event_date: date | None
    source_message_id: uuid.UUID | None


@dataclass(frozen=True)
class Outcome:
    """Итог одной задачи — без текстов (для журнала и тестов)."""

    kind: Literal["extract", "compact", "forget"]
    status: Status
    inserted: int = 0
    merged: int = 0
    repeated: int = 0
    forgotten: int = 0
    dropped: int = 0  # дубликаты, убранные сжатием
    error: str | None = None
    calls: tuple[CallRecord, ...] = field(default_factory=tuple)


class _Gone(Exception):
    """Агента нет (владелец удалён во время работы) — штатный исход (Б-44)."""


@asynccontextmanager
async def _owner_write(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> AsyncIterator[AsyncSession]:
    """Транзакция записи от имени владельца; первой берётся строка агента (порядок блокировок
    «агент → записи памяти → диалоги», ревью 2.4 р2 MAJOR-1)."""
    async with user_scope(sm, user_id) as s:
        if not await mw.lock_agent_in(s, user_id):
            raise _Gone
        yield s


class _Rollback(Exception):
    """Завершение вернуло false или запись изменилась: откатить транзакцию владельца."""


def _provider(models: AgentModels) -> Any:
    return models.summary if models.summary is not None else models.safety


# --------------------------------------------------------------------------- вызовы модели

EXTRACT_SCHEMA: dict[str, Any] = strict_object(
    {
        "items": {
            "type": "array",
            "items": strict_object(
                {
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "text": {"type": "string"},
                    "event_date": nullable({"type": "string"}),
                    "confidence": {"type": "number"},
                    "partner_ref": nullable({"type": "string"}),
                }
            ),
        }
    }
)
MERGE_SCHEMA: dict[str, Any] = strict_object(
    {"same": {"type": "boolean"}, "text": {"type": "string"}}
)
PROFILE_SCHEMA: dict[str, Any] = strict_object({"profile": {"type": "string"}})


def clip(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def extract_payload(
    owner_turns: Sequence[SourceText], partner: Sequence[SourceText]
) -> tuple[dict[str, Any], dict[str, uuid.UUID]]:
    """Вход модели — только тексты, без идентификаторов; ``ref`` сообщений — локальные метки."""
    refs = {f"m{i + 1}": m.id for i, m in enumerate(partner)}
    payload = {
        "owner_turns": [t.text for t in owner_turns],
        "partner_messages": [
            {"ref": r, "text": m.text} for r, m in zip(refs, partner, strict=True)
        ],
    }
    return payload, refs


def _date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def candidates_from(
    data: dict[str, Any], refs: dict[str, uuid.UUID], mode: Literal["owner", "partner"]
) -> list[Candidate]:
    """Проверка ответа в коде; **источник задаёт код по виду вызова** (ревью 2.4 MINOR-2):

    - ``owner`` (вход — только реплики владельца): виды владельца и наблюдения; ``from_partner`` и
      ссылки на сообщения отбрасываются;
    - ``partner`` (вход — только доставленные сообщения партнёра): любая запись становится
      ``from_partner`` со ссылкой на своё сообщение — запись без верной ссылки отбрасывается; факт
      из текста партнёра не получит источник «слова владельца».

    Структура неверна — ``ValueError``/``TypeError`` (→ ``bad_output``)."""
    items = data["items"]
    if not isinstance(items, list):
        raise TypeError("items")
    out: list[Candidate] = []
    for item in items[:MAX_ITEMS]:
        if not isinstance(item, dict) or item.get("kind") not in KINDS:
            raise ValueError("kind")
        kind = str(item["kind"])
        body = item.get("text")
        conf = item.get("confidence")
        if not isinstance(body, str) or not body.strip():
            continue
        if isinstance(conf, bool) or not isinstance(conf, int | float) or not 0 <= conf <= 1:
            raise ValueError("confidence")
        ref = item.get("partner_ref")
        message: uuid.UUID | None = None
        if mode == "partner":
            if not isinstance(ref, str) or ref not in refs:
                continue
            kind, message = "from_partner", refs[ref]
        elif kind == "from_partner" or ref is not None:
            continue
        confidence = float(conf)
        if kind == "observation":
            confidence = min(confidence, OBSERVATION_MAX_CONFIDENCE)
        out.append(
            Candidate(
                kind=kind,
                source=SOURCE_OF.get(kind, "owner_dialog"),
                text=clip(body, MAX_ITEM_CHARS),
                confidence=round(confidence, 3),
                event_date=_date(item.get("event_date")),
                source_message_id=message,
            )
        )
    return out


def batches(
    items: Sequence[SourceText], *, max_items: int, max_chars: int
) -> list[list[SourceText]]:
    """Пачки по числу и длине (хотя бы один текст в пачке; длинный текст — обрезан): длинная
    сессия не превышает окно модели и не теряется после предела попыток (MINOR-3)."""
    out: list[list[SourceText]] = []
    cur: list[SourceText] = []
    size = 0
    for it in items:
        t = SourceText(it.id, it.text[:max_chars])
        if cur and (len(cur) >= max_items or size + len(t.text) > max_chars):
            out.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += len(t.text)
    if cur:
        out.append(cur)
    return out


async def extract(
    models: AgentModels,
    owner_turns: Sequence[SourceText],
    partner: Sequence[SourceText],
    *,
    log: CallLog,
    version: str = EXTRACT_VERSION,
    max_items: int = BATCH_ITEMS,
    max_chars: int = BATCH_CHARS,
) -> list[Candidate]:
    """Отдельные вызовы: по пачкам реплик владельца и по пачкам сообщений партнёра."""
    out: list[Candidate] = []
    jobs: list[tuple[Literal["owner", "partner"], list[SourceText]]] = [
        ("owner", b) for b in batches(owner_turns, max_items=max_items, max_chars=max_chars)
    ] + [("partner", b) for b in batches(partner, max_items=max_items, max_chars=max_chars)]
    for mode, chunk in jobs:
        payload, refs = (
            extract_payload(chunk, []) if mode == "owner" else extract_payload([], chunk)
        )

        def validate(
            d: dict[str, Any],
            refs: dict[str, uuid.UUID] = refs,
            mode: Literal["owner", "partner"] = mode,
        ) -> list[Candidate]:
            return candidates_from(d, refs, mode)

        out += await complete(
            _provider(models),
            prompt=load("memory_extract", version),
            payload=payload,
            schema=EXTRACT_SCHEMA,
            validate=validate,
            log=log,
            max_tokens=MAX_TOKENS,
        )
    return out


async def merge_texts(
    models: AgentModels, existing: str, new: str, *, log: CallLog, version: str = MERGE_VERSION
) -> str | None:
    """Одна запись из двух (или ``None`` — содержание разное)."""

    def validate(data: dict[str, Any]) -> str | None:
        same = data["same"]
        if not isinstance(same, bool):
            raise TypeError("same")
        if not same:
            return None
        return clip(text_field(data, "text"), MAX_ITEM_CHARS)

    return await complete(
        _provider(models),
        prompt=load("memory_merge", version),
        payload={"existing": existing, "new": new},
        schema=MERGE_SCHEMA,
        validate=validate,
        log=log,
        max_tokens=MAX_TOKENS,
    )


async def build_profile(
    models: AgentModels,
    items: Sequence[tuple[str, str, date | None]],
    *,
    log: CallLog,
    version: str = PROFILE_VERSION,
) -> str:
    """Сводка профиля из **живых** записей (вид, текст, дата) — удалённого в ней нет (АР-31)."""
    if not items:
        return ""
    return await complete(
        _provider(models),
        prompt=load("profile_summary", version),
        payload={
            "records": [
                {"kind": k, "text": t, "event_date": d.isoformat() if d else None}
                for k, t, d in items
            ]
        },
        schema=PROFILE_SCHEMA,
        validate=lambda d: clip(text_field(d, "profile", allow_empty=True), MAX_PROFILE_CHARS),
        log=log,
        max_tokens=MAX_TOKENS,
    )


async def _vectors(embedder: Embedder | None, texts: Sequence[str]) -> list[list[float] | None]:
    """Векторы текстов (ошибка эмбеддинга — без вектора: запись всё равно сохраняется)."""
    if embedder is None or not texts:
        return [None] * len(texts)
    try:
        check_model(embedder.model)
        raw = await embedder.embed(list(texts))
    except EmbeddingError:
        return [None] * len(texts)
    out: list[list[float] | None] = []
    for v in raw:
        try:
            out.append(check_dim(v))
        except EmbeddingError:
            out.append(None)
    return out


def _mergeable(item: LiveMemory, kind: str, source: str, message: uuid.UUID | None) -> bool:
    """Сливаются только записи одного вида и источника (и одного сообщения партнёра)."""
    return item.type == kind and item.source == source and item.source_message_id == message


# --------------------------------------------------------------------------- извлечение


async def _finish_extraction(sm: async_sessionmaker[AsyncSession], claim: ExtractionClaim) -> bool:
    async with _owner_write(sm, claim.user_id) as s:
        if not await mw.extraction_done_in(
            s,
            claim.token,
            claim.session_through[1] if claim.session_through else None,
            claim.partner_through[1] if claim.partner_through else None,
        ):
            raise _Rollback
    return True


@dataclass
class _Merge:
    target: LiveMemory
    text: str
    candidates: list[int]
    confidence: float
    event_date: date | None


async def run_extraction(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    fp: FingerprintKeys,
    models: AgentModels,
    claim: ExtractionClaim,
    *,
    embedder: Embedder | None = None,
    max_attempts: int = 5,
    merge_distance: float = MERGE_DISTANCE,
) -> Outcome:
    log = CallLog()

    def outcome(status: Status, **kw: Any) -> Outcome:
        return Outcome("extract", status, calls=tuple(log.records), **kw)

    try:
        if claim.attempts > max_attempts:
            # Сессия раз за разом не обрабатывается — завершаем без извлечения (не зацикливаемся).
            await _finish_extraction(sm, claim)
            return outcome("skipped")
        async with user_scope(sm, claim.user_id) as s:
            turns = await mw.session_owner_turns(s, keys, claim.user_id, claim)
            partner = await mw.session_partner_messages(s, keys, claim.user_id, claim)
        if not turns and not partner:
            await _finish_extraction(sm, claim)
            return outcome("empty")
        try:
            found = await extract(models, turns, partner, log=log)
        except LLMRefusal:
            # Отказ модели повторится — сессия завершается без записей (памяти не будет, но и
            # ничего лишнего); недоступность и испорченный ответ — повтор после аренды.
            await _finish_extraction(sm, claim)
            return outcome("refused", error="refusal")
        except LLMError as exc:
            return outcome("error", error=exc.kind)
        unique: dict[bytes, Candidate] = {}
        for c in found:
            unique.setdefault(fp.of(claim.agent_id, c.text), c)
        cands = list(unique.values())
        vectors = await _vectors(embedder, [c.text for c in cands])
        model = embedder.model if embedder is not None else None

        # Слияние планируется вне транзакции записи (модель — вне транзакций).
        merges: dict[uuid.UUID, _Merge] = {}
        merged_into: dict[int, uuid.UUID] = {}
        if model is not None and any(v is not None for v in vectors):
            async with user_scope(sm, claim.user_id) as s:
                live = {m.id: m for m in await mw.live_memory(s, keys, claim.user_id)}
                near = {
                    i: await mw.nearest_in(s, v, model, NEIGHBOURS)
                    for i, v in enumerate(vectors)
                    if v is not None
                }
            for i, c in enumerate(cands):
                for item_id, dist in near.get(i, []):
                    item = live.get(item_id)
                    if item is None or dist > merge_distance:
                        continue
                    if not _mergeable(item, c.kind, c.source, c.source_message_id):
                        continue
                    if item.fingerprint is not None and item.fingerprint in fp.all_of(
                        claim.agent_id, c.text
                    ):
                        break  # точный повтор — разберёт запись по отпечатку
                    plan = merges.get(item.id)
                    base = plan.text if plan else item.text
                    try:
                        text = await merge_texts(models, base, c.text, log=log)
                    except LLMError as exc:
                        return outcome("error", error=exc.kind)
                    if text is None:
                        continue
                    if plan is None:
                        plan = _Merge(item, text, [], item.confidence, item.event_date)
                        merges[item.id] = plan
                    plan.text = text
                    plan.candidates.append(i)
                    plan.confidence = max(plan.confidence, c.confidence)
                    plan.event_date = c.event_date or plan.event_date
                    merged_into[i] = item.id
                    break
        merge_vectors = dict(
            zip(
                merges,
                await _vectors(embedder, [m.text for m in merges.values()]),
                strict=True,
            )
        )

        counts = {"inserted": 0, "merged": 0, "repeated": 0, "forgotten": 0}
        async with _owner_write(sm, claim.user_id) as s:
            texts = [c.text for c in cands] + [m.text for m in merges.values()]
            forgotten = await mw.forgotten_in(
                s, [f for t in texts for f in fp.all_of(claim.agent_id, t)]
            )

            def is_forgotten(body: str) -> bool:
                return any(f in forgotten for f in fp.all_of(claim.agent_id, body))

            existing = await mw.live_by_fingerprint_in(
                s, claim.user_id, [f for c in cands for f in fp.all_of(claim.agent_id, c.text)]
            )
            pending_insert: list[int] = []
            for i, c in enumerate(cands):
                if is_forgotten(c.text):
                    counts["forgotten"] += 1
                    merged_into.pop(i, None)
                    continue
                same = next(
                    (existing[f] for f in fp.all_of(claim.agent_id, c.text) if f in existing),
                    None,
                )
                if same is not None:
                    await mw.raise_confidence_in(s, claim.user_id, same, c.confidence)
                    counts["repeated"] += 1
                    merged_into.pop(i, None)
                    continue
                if i not in merged_into:
                    pending_insert.append(i)
            for plan in merges.values():
                live_parts = [i for i in plan.candidates if merged_into.get(i) == plan.target.id]
                if not live_parts:
                    continue
                ok = not is_forgotten(plan.text) and await mw.update_text_in(
                    s,
                    keys,
                    claim.user_id,
                    plan.target.id,
                    body=plan.text,
                    fingerprint=fp.of(claim.agent_id, plan.text),
                    confidence=plan.confidence,
                    event_date=plan.event_date,
                    expected_version=plan.target.version,
                )
                if not ok:
                    # Запись изменили или удалили после чтения (или итог совпал с удалённой):
                    # её текст в слияние не берём — кандидаты сохраняются сами по себе.
                    pending_insert.extend(live_parts)
                    continue
                counts["merged"] += len(live_parts)
                vec = merge_vectors.get(plan.target.id)
                if vec is not None and model is not None:
                    await mw.set_embedding_in(s, plan.target.id, vec, model)
            for i in sorted(pending_insert):
                c = cands[i]
                new_id = await mw.insert_in(
                    s,
                    keys,
                    claim.agent_id,
                    memory_type=c.kind,
                    source=c.source,
                    body=c.text,
                    fingerprint=fp.of(claim.agent_id, c.text),
                    confidence=c.confidence,
                    event_date=c.event_date,
                    source_message_id=c.source_message_id,
                )
                counts["inserted"] += 1
                if vectors[i] is not None and model is not None:
                    await mw.set_embedding_in(s, new_id, vectors[i], model)  # type: ignore[arg-type]
            if not await mw.extraction_done_in(
                s,
                claim.token,
                claim.session_through[1] if claim.session_through else None,
                claim.partner_through[1] if claim.partner_through else None,
            ):
                raise _Rollback
        return outcome("done", **counts)
    except _Rollback:
        return outcome("lost")
    except _Gone:
        return outcome("gone")
    except DBAPIError as exc:
        if mw.owner_gone(exc):
            return outcome("gone")
        raise


# --------------------------------------------------------------------------- сжатие


def forgotten_summary(old: HistorySummary) -> str | None:
    """Сводка диалога без текста, с сохранённой отметкой (АР-37: приватность важнее
    непрерывности). Реплики после отметки по-прежнему идут в контекст."""
    if old.through_at is None:
        return None
    return pack_summary(HistorySummary("", old.through_at, old.through_id))


async def run_compaction(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    fp: FingerprintKeys,
    models: AgentModels,
    claim: CompactionClaim,
    *,
    embedder: Embedder | None = None,
    max_attempts: int = 5,
    merge_distance: float = MERGE_DISTANCE,
) -> Outcome:
    log = CallLog()

    def outcome(status: Status, **kw: Any) -> Outcome:
        return Outcome("compact", status, calls=tuple(log.records), **kw)

    async def finish_only() -> None:
        async with _owner_write(sm, claim.user_id) as s:
            if not await mw.compaction_done_in(s, claim.token):
                raise _Rollback

    try:
        if claim.attempts > max_attempts:
            await finish_only()
            return outcome("skipped")
        model = embedder.model if embedder is not None else None
        async with user_scope(sm, claim.user_id) as s:
            items = await mw.live_memory(s, keys, claim.user_id, model)
            near: dict[uuid.UUID, list[tuple[uuid.UUID, float]]] = {}
            if model is not None:
                for m in items:
                    if m.embedding is not None:
                        near[m.id] = await mw.nearest_in(s, m.embedding, model, NEIGHBOURS + 1)
            # Сводки «на очистку» (АР-37), которые задача очистки не успела переписать
            # (перезапуск, сбой), — обнуляются здесь с сохранением отметки.
            summaries = [
                (cid, raw)
                for cid, raw in await mw.conversation_summaries_in(s, keys, claim.user_id)
                if unpack_summary(raw).forget
            ]
        by_id = {m.id: m for m in items}

        # 1) Дубликаты по отпечатку (один вид, источник, сообщение): остаётся самая ранняя.
        drop: dict[uuid.UUID, LiveMemory] = {}
        survivors: dict[uuid.UUID, tuple[str, float, date | None]] = {}
        groups: dict[tuple[Any, ...], list[LiveMemory]] = {}
        for m in items:
            key_fp = m.fingerprint or fp.of(claim.agent_id, m.text)
            groups.setdefault((key_fp, m.type, m.source, m.source_message_id), []).append(m)
        for group in groups.values():
            first, rest = group[0], group[1:]
            for m in rest:
                drop[m.id] = m
            if rest:
                best = max(x.confidence for x in group)
                date_ = next((x.event_date for x in group if x.event_date), None)
                survivors[first.id] = (first.text, best, date_)

        # 2) Близкие по смыслу пары — модель слияния.
        for m in items:
            if m.id in drop:
                continue
            for other_id, dist in near.get(m.id, []):
                other = by_id.get(other_id)
                if other is None or other.id == m.id or other.id in drop or dist > merge_distance:
                    continue
                if other.id in survivors and m.id not in survivors:
                    continue
                if not _mergeable(other, m.type, m.source, m.source_message_id):
                    continue
                base = survivors.get(m.id, (m.text, m.confidence, m.event_date))
                try:
                    text = await merge_texts(models, base[0], other.text, log=log)
                except LLMError as exc:
                    return outcome("error", error=exc.kind)
                if text is None:
                    continue
                survivors[m.id] = (
                    text,
                    max(base[1], other.confidence),
                    base[2] or other.event_date,
                )
                drop[other.id] = other

        # 3) Сводка профиля — из итоговых живых записей.
        final: list[tuple[str, str, date | None]] = []
        for m in items:
            if m.id in drop:
                continue
            body, _, when = survivors.get(m.id, (m.text, m.confidence, m.event_date))
            final.append((m.type, body, when))
        try:
            profile = await build_profile(models, final, log=log)
        except LLMError as exc:
            return outcome("error", error=exc.kind)
        changed_vectors = dict(
            zip(
                survivors,
                await _vectors(embedder, [v[0] for v in survivors.values()]),
                strict=True,
            )
        )

        merged = 0
        async with _owner_write(sm, claim.user_id) as s:
            # Всё, на чём построены слияние и сводка, должно быть живо и не изменено: иначе
            # (владелец удалил запись во время сжатия) — откат, следующий раунд.
            now_live = {m.id: m.version for m in await mw.live_memory(s, keys, claim.user_id)}
            if any(now_live.get(m.id) != m.version for m in items):
                raise _Rollback
            check = [f for v in survivors.values() for f in fp.all_of(claim.agent_id, v[0])]
            gone_fps = await mw.forgotten_in(s, check)
            for item_id, (text, conf, date_) in survivors.items():
                item = by_id[item_id]
                if any(f in gone_fps for f in fp.all_of(claim.agent_id, text)):
                    raise _Rollback  # итог слияния совпал с удалённой записью — не сохраняем
                if text == item.text:
                    if not await mw.raise_confidence_in(s, claim.user_id, item_id, conf):
                        raise _Rollback
                    continue
                if not await mw.update_text_in(
                    s,
                    keys,
                    claim.user_id,
                    item_id,
                    body=text,
                    fingerprint=fp.of(claim.agent_id, text),
                    confidence=conf,
                    event_date=date_,
                    expected_version=item.version,
                ):
                    raise _Rollback
                merged += 1
                vec = changed_vectors.get(item_id)
                if vec is not None and model is not None:
                    await mw.set_embedding_in(s, item_id, vec, model)
            for item in drop.values():
                if not await mw.drop_duplicate_in(s, claim.user_id, item.id, item.version):
                    raise _Rollback
            await mw.set_profile_summary_in(s, keys, claim.user_id, claim.agent_id, profile)
            for conv_id, raw in summaries:
                # Сверка с прочитанным (MINOR-5): сводку переписали — пропуск.
                await mw.replace_summary_in(
                    s,
                    keys,
                    claim.user_id,
                    conv_id,
                    expected_raw=raw,
                    new_raw=forgotten_summary(unpack_summary(raw)),
                )
            if not await mw.compaction_done_in(s, claim.token):
                raise _Rollback
        return outcome("done", merged=merged, dropped=len(drop))
    except _Rollback:
        return outcome("lost")
    except _Gone:
        return outcome("gone")
    except DBAPIError as exc:
        if mw.owner_gone(exc):
            return outcome("gone")
        raise


# --------------------------------------------------------------------------- удаление (АР-37)

FORGET_SCHEMA: dict[str, Any] = strict_object({"rewritten": {"type": "string"}})


@dataclass(frozen=True)
class ForgetJob:
    """Задача очистки сводок после удаления записи памяти. Текст удалённой записи живёт **только
    в памяти процесса** (в БД его уже нет); в журналы и ``repr`` не попадает."""

    user_id: uuid.UUID
    agent_id: uuid.UUID
    item_id: uuid.UUID
    text: str = field(repr=False)


async def forget_memory(
    sm: async_sessionmaker[AsyncSession], keys: Keyring, user_id: uuid.UUID, item_id: uuid.UUID
) -> ForgetJob | None:
    """Удаление записи памяти владельцем (``DELETE /memory/{id}``, ФТ-13; АР-37) — одна
    транзакция: прочитать и расшифровать текст (до удаления), ``app_delete_memory`` (отпечаток
    остаётся), обнулить сводку профиля, пометить сводки диалогов «на очистку» (в контекст и
    выгрузку не идут). ``None`` — записи нет среди своих живых (404, неразличимо). Затем API
    запускает ``run_forget(job)`` в фоне (не дожидаясь); если она не выполнится, помеченные сводки
    обнулит сжатие памяти (``run_compaction``)."""
    try:
        async with _owner_write(sm, user_id) as s:
            taken = await mw.take_for_forget_in(s, keys, user_id, item_id)
            if taken is None:
                return None
            agent_id, body = taken
            if not await mw.delete_memory_in(s, item_id):
                return None
            await mw.clear_profile_summary_in(s, user_id)
            for conv_id, raw in await mw.conversation_summaries_in(s, keys, user_id):
                old = unpack_summary(raw)
                if not old.text:
                    continue
                marked = HistorySummary(
                    old.text, old.through_at, old.through_id, (*old.forget, str(item_id))
                )
                await mw.replace_summary_in(
                    s, keys, user_id, conv_id, expected_raw=raw, new_raw=pack_summary(marked)
                )
    except _Gone:
        return None
    except DBAPIError as exc:
        if mw.owner_gone(exc):
            return None
        raise
    return ForgetJob(user_id, agent_id, item_id, body)


async def rewrite_without(
    models: AgentModels, summary: str, forget: str, *, log: CallLog, version: str = FORGET_VERSION
) -> str:
    """Сводка без удалённого сведения (``summary_forget.v0``, черновик без прогона, АР-26)."""
    return await complete(
        _provider(models),
        prompt=load("summary_forget", version),
        payload={"summary": summary, "forget": forget},
        schema=FORGET_SCHEMA,
        validate=lambda d: text_field(d, "rewritten", allow_empty=True),
        log=log,
        max_tokens=MAX_TOKENS,
    )


def leaks(summary: str, forgotten: str) -> bool:
    """Удалённый текст дословно остался в сводке — после нормализации АР-36 (регистр, «ё»,
    пунктуация, пробелы): целиком или как предложение. Перефразировку не ловит."""
    target = normalize(forgotten)
    return bool(target) and f" {target} " in f" {normalize(summary)} "


async def run_forget(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    models: AgentModels,
    job: ForgetJob,
) -> Outcome:
    """Фоновая очистка после ``forget_memory``: каждая сводка диалога с пометкой этой записи
    переписывается моделью без удалённого (пометка этой записи снимается; другие — остаются); при
    ошибке модели сводка обнуляется с сохранением отметки. Запись — со сверкой (сводку могли
    переписать — перечитать, до ``FORGET_RETRIES`` раз). Затем сводка профиля пересобирается из
    живых записей (если с чтения ничего не удалили и не изменили)."""
    log = CallLog()
    mark = str(job.item_id)
    cleaned = 0
    try:
        for _ in range(FORGET_RETRIES):
            async with user_scope(sm, job.user_id) as s:
                pending = [
                    (cid, raw)
                    for cid, raw in await mw.conversation_summaries_in(s, keys, job.user_id)
                    if mark in unpack_summary(raw).forget
                ]
            if not pending:
                break
            for conv_id, raw in pending:
                old = unpack_summary(raw)
                rest = tuple(x for x in old.forget if x != mark)
                try:
                    text = await rewrite_without(models, old.text, job.text, log=log)
                    if leaks(text, job.text):
                        text = ""  # модель оставила удалённое дословно — сводка пустая (MINOR-1)
                    new_raw: str | None = pack_summary(
                        HistorySummary(text, old.through_at, old.through_id, rest)
                    )
                except LLMError:
                    new_raw = forgotten_summary(old)
                async with _owner_write(sm, job.user_id) as s:
                    if await mw.replace_summary_in(
                        s, keys, job.user_id, conv_id, expected_raw=raw, new_raw=new_raw
                    ):
                        cleaned += 1
        async with user_scope(sm, job.user_id) as s:
            items = await mw.live_memory(s, keys, job.user_id)
        try:
            profile = await build_profile(
                models, [(m.type, m.text, m.event_date) for m in items], log=log
            )
        except LLMError as exc:
            return Outcome(
                "forget", "error", merged=cleaned, error=exc.kind, calls=tuple(log.records)
            )
        async with _owner_write(sm, job.user_id) as s:
            now = {m.id: m.version for m in await mw.live_memory(s, keys, job.user_id)}
            if now == {m.id: m.version for m in items}:
                await mw.set_profile_summary_in(s, keys, job.user_id, job.agent_id, profile)
        return Outcome("forget", "done", merged=cleaned, calls=tuple(log.records))
    except _Gone:
        return Outcome("forget", "gone", calls=tuple(log.records))
    except DBAPIError as exc:
        if mw.owner_gone(exc):
            return Outcome("forget", "gone", calls=tuple(log.records))
        raise

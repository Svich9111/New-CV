"""Слой доступа фоновых задач долговременной памяти (ТЗ §6.3, ФТ-12, ФТ-14; задача 2.4, АР-36).

Две области:

- **воркер** (``worker_scope``: ``SET LOCAL ROLE feelings_worker``) — только захват работы
  функциями ``memory_claim_extraction`` / ``memory_claim_compaction`` короткой транзакцией: id,
  отметки и аренда, без текстов;
- **владелец** (``user_scope`` → ``feelings_app``, RLS + явный отбор по владельцу) — чтение реплик
  сессии и доставленных владельцу сообщений пары, чтение и запись своей памяти, проверка
  отпечатков удалённых записей (``app_memory_forgotten``), векторы (``app_set_memory_embedding``)
  и завершение (``app_memory_extraction_done`` / ``app_memory_compaction_done``) — в одной
  транзакции, завершение последним; ``false`` — откат (функции ``_in(s, …)`` принимают открытую
  сессию владельца).

Отдельный модуль, а не ``repositories/memory.py`` (эндпоинты ``/memory``, задача 2.4 этап 2, A2).
Тексты в журналы не пишутся. Владелец удалён во время работы — 42501/23503, вызывающий
переводит в ``OwnerGone`` (Б-44)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import to_pgvector
from app.core import crypto
from app.core.crypto import Keyring
from app.db import sqlstate
from app.repositories.chat import DELIVERED
from app.repositories.texts import open_row

WORKER_ROLE = "feelings_worker"
FORGOTTEN_BATCH = 1000  # предел app_memory_forgotten (22023 больше)
OWNER_GONE_SQLSTATES = frozenset({"42501", "23503"})

Mark = tuple[datetime, uuid.UUID]


def owner_gone(exc: DBAPIError) -> bool:
    """Владелец удалён посреди работы (функции — 42501, вставка без агента — 23503)."""
    return sqlstate(exc) in OWNER_GONE_SQLSTATES


def _uuid(value: object) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _mark(at: datetime | None, rid: object) -> Mark | None:
    return None if at is None or rid is None else (at, _uuid(rid))


# --------------------------------------------------------------------------- захват (воркер)


@asynccontextmanager
async def worker_scope(sm: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Короткая транзакция воркера под ``feelings_worker`` (роль входа — член ``feelings_worker`` и
    ``feelings_app``, Б-69). Только функции захвата: к таблицам у роли доступа нет."""
    async with sm() as session, session.begin():
        await session.execute(text(f"SET LOCAL ROLE {WORKER_ROLE}"))
        yield session


@dataclass(frozen=True)
class ExtractionClaim:
    user_id: uuid.UUID
    agent_id: uuid.UUID
    token: uuid.UUID
    attempts: int
    conversation_id: uuid.UUID | None
    session_after: Mark | None
    session_through: Mark | None
    partner_after: Mark | None
    partner_through: Mark | None


@dataclass(frozen=True)
class CompactionClaim:
    user_id: uuid.UUID
    agent_id: uuid.UUID
    token: uuid.UUID
    attempts: int
    compacted_at: datetime | None


async def claim_extraction(
    sm: async_sessionmaker[AsyncSession], *, gap: timedelta, lease: timedelta, limit: int
) -> list[ExtractionClaim]:
    async with worker_scope(sm) as s:
        rows = (
            await s.execute(
                text("SELECT * FROM memory_claim_extraction(:g, :l, :n)"),
                {"g": gap, "l": lease, "n": limit},
            )
        ).all()
    return [
        ExtractionClaim(
            user_id=_uuid(r.user_id),
            agent_id=_uuid(r.agent_id),
            token=_uuid(r.token),
            attempts=int(r.attempts),
            conversation_id=None if r.conversation_id is None else _uuid(r.conversation_id),
            session_after=_mark(r.session_after_at, r.session_after_id),
            session_through=_mark(r.session_through_at, r.session_through_id),
            partner_after=_mark(r.partner_after_at, r.partner_after_id),
            partner_through=_mark(r.partner_through_at, r.partner_through_id),
        )
        for r in rows
    ]


async def claim_compaction(
    sm: async_sessionmaker[AsyncSession], *, every: timedelta, lease: timedelta, limit: int
) -> list[CompactionClaim]:
    async with worker_scope(sm) as s:
        rows = (
            await s.execute(
                text("SELECT * FROM memory_claim_compaction(:e, :l, :n)"),
                {"e": every, "l": lease, "n": limit},
            )
        ).all()
    return [
        CompactionClaim(
            user_id=_uuid(r.user_id),
            agent_id=_uuid(r.agent_id),
            token=_uuid(r.token),
            attempts=int(r.attempts),
            compacted_at=r.compacted_at,
        )
        for r in rows
    ]


# --------------------------------------------------------------------------- чтение (владелец)


@dataclass(frozen=True)
class SourceText:
    id: uuid.UUID
    text: str


async def session_owner_turns(
    s: AsyncSession, keys: Keyring, user_id: uuid.UUID, claim: ExtractionClaim
) -> list[SourceText]:
    """Реплики **владельца** сессии ``(after, through]`` своего диалога (ответы агента — нет)."""
    if claim.conversation_id is None or claim.session_through is None:
        return []
    params: dict[str, object] = {
        "c": claim.conversation_id,
        "u": user_id,
        "ta": claim.session_through[0],
        "ti": claim.session_through[1],
    }
    after = ""
    if claim.session_after is not None:
        after = "AND (m.created_at, m.id) > (:aa, :ai) "
        params |= {"aa": claim.session_after[0], "ai": claim.session_after[1]}
    rows = await s.execute(
        text(
            "SELECT m.id, m.text_enc FROM conversation_messages m "  # noqa: S608 — константы
            "JOIN conversations c ON c.id = m.conversation_id JOIN agents a ON a.id = c.agent_id "
            "WHERE m.conversation_id = :c AND a.user_id = :u AND m.author = 'owner' "
            f"{after}AND (m.created_at, m.id) <= (:ta, :ti) ORDER BY m.created_at, m.id"
        ),
        params,
    )
    out = []
    for r in rows:
        body, _ = open_row(keys, crypto.CONVERSATION_MESSAGE_TEXT, _uuid(r.id), r.text_enc)
        if body and body.strip():
            out.append(SourceText(_uuid(r.id), body))
    return out


async def session_partner_messages(
    s: AsyncSession, keys: Keyring, user_id: uuid.UUID, claim: ExtractionClaim
) -> list[SourceText]:
    """Одобренные автором и **доставленные владельцу** сообщения **текущей активной пары**
    ``(after, through]`` по ``(delivered_at, id)`` — версия, которую владелец видел
    (``adapted_enc``, ФТ-14, АР-37). Сообщения распавшейся пары пропускаются (отметка всё равно
    двигается). Личный чат и память партнёра недоступны: их нет в выборке, RLS их не отдаёт."""
    if claim.partner_through is None:
        return []
    params: dict[str, object] = {
        "u": user_id,
        "st": list(DELIVERED),
        "ta": claim.partner_through[0],
        "ti": claim.partner_through[1],
    }
    after = ""
    if claim.partner_after is not None:
        after = "AND (pm.delivered_at, pm.id) > (:aa, :ai) "
        params |= {"aa": claim.partner_after[0], "ai": claim.partner_after[1]}
    rows = await s.execute(
        text(
            "SELECT pm.id, pm.adapted_enc FROM partner_messages pm "  # noqa: S608 — константы
            "WHERE pm.recipient_id = :u AND pm.author_id <> :u "
            # АР-37 (ФТ-14): только текущая активная пара, как контекст 2.1.
            "AND pm.couple_id IN (SELECT cm.couple_id FROM couple_members cm "
            "JOIN couples c ON c.id = cm.couple_id "
            "WHERE cm.user_id = :u AND cm.active AND c.status = 'active') "
            "AND pm.status::text = ANY(CAST(:st AS text[])) AND pm.delivered_at IS NOT NULL "
            f"{after}AND (pm.delivered_at, pm.id) <= (:ta, :ti) "
            "ORDER BY pm.delivered_at, pm.id"
        ),
        params,
    )
    out = []
    for r in rows:
        if r.adapted_enc is None:
            continue
        msg = _uuid(r.id)
        body, _ = open_row(keys, crypto.MESSAGE_ADAPTED, msg, r.adapted_enc)
        if body and body.strip():
            out.append(SourceText(msg, body))
    return out


@dataclass(frozen=True)
class LiveMemory:
    id: uuid.UUID
    type: str
    source: str
    source_message_id: uuid.UUID | None
    text: str
    confidence: float
    event_date: date | None
    fingerprint: bytes | None
    version: datetime  # COALESCE(updated_at, created_at) — для проверки «не изменилась»
    embedding: str | None  # вектор текстом pgvector (только своей модели) — для поиска соседей


async def live_memory(
    s: AsyncSession, keys: Keyring, user_id: uuid.UUID, model: str | None = None
) -> list[LiveMemory]:
    """Живые записи своей памяти (по времени). ``embedding`` — только векторы модели ``model``."""
    rows = await s.execute(
        text(
            "SELECT m.id, m.type::text AS type, m.source::text AS source, m.source_message_id, "
            "m.text_enc, m.confidence, m.event_date, m.fingerprint, "
            "COALESCE(m.updated_at, m.created_at) AS version, "
            "CASE WHEN m.embedding_model = :m THEN m.embedding END AS embedding "
            "FROM memory_items m JOIN agents a ON a.id = m.agent_id "
            "WHERE a.user_id = :u AND m.deleted_at IS NULL AND m.text_enc IS NOT NULL "
            "ORDER BY m.created_at, m.id"
        ),
        {"u": user_id, "m": model},
    )
    out = []
    for r in rows:
        rid = _uuid(r.id)
        body, _ = open_row(keys, crypto.MEMORY_TEXT, rid, r.text_enc)
        if body is None:
            continue
        out.append(
            LiveMemory(
                id=rid,
                type=r.type,
                source=r.source,
                source_message_id=None
                if r.source_message_id is None
                else _uuid(r.source_message_id),
                text=body,
                confidence=float(r.confidence),
                event_date=r.event_date,
                fingerprint=None if r.fingerprint is None else bytes(r.fingerprint),
                version=r.version,
                embedding=None if r.embedding is None else str(r.embedding),
            )
        )
    return out


async def nearest_in(
    s: AsyncSession, vector: Sequence[float] | str, model: str, limit: int
) -> list[tuple[uuid.UUID, float]]:
    """Ближайшие свои записи (``app_memory_nearest``); отказ БД (22023) — пусто, транзакция цела."""
    query = vector if isinstance(vector, str) else to_pgvector(vector)
    try:
        async with s.begin_nested():
            rows = (
                await s.execute(
                    text("SELECT id, distance FROM app_memory_nearest(:q, :m, :n)"),
                    {"q": query, "m": model, "n": limit},
                )
            ).all()
    except DBAPIError as exc:
        if sqlstate(exc) != "22023":
            raise
        return []
    return [(_uuid(r.id), float(r.distance)) for r in rows]


async def forgotten_in(s: AsyncSession, fingerprints: Iterable[bytes]) -> set[bytes]:
    """Какие из отпечатков принадлежат удалённым записям своего агента (АР-36)."""
    items = list(dict.fromkeys(fingerprints))
    found: set[bytes] = set()
    for i in range(0, len(items), FORGOTTEN_BATCH):
        rows = await s.execute(
            text("SELECT fingerprint FROM app_memory_forgotten(CAST(:f AS bytea[]))"),
            {"f": items[i : i + FORGOTTEN_BATCH]},
        )
        found |= {bytes(r.fingerprint) for r in rows}
    return found


async def live_by_fingerprint_in(
    s: AsyncSession, user_id: uuid.UUID, fingerprints: Iterable[bytes]
) -> dict[bytes, uuid.UUID]:
    items = list(dict.fromkeys(fingerprints))
    if not items:
        return {}
    rows = await s.execute(
        text(
            "SELECT m.fingerprint, m.id FROM memory_items m JOIN agents a ON a.id = m.agent_id "
            "WHERE a.user_id = :u AND m.deleted_at IS NULL AND m.text_enc IS NOT NULL "
            "AND m.fingerprint = ANY(CAST(:f AS bytea[])) ORDER BY m.created_at, m.id"
        ),
        {"u": user_id, "f": items},
    )
    out: dict[bytes, uuid.UUID] = {}
    for r in rows:
        out.setdefault(bytes(r.fingerprint), _uuid(r.id))
    return out


# --------------------------------------------------------------------------- запись (владелец)


async def insert_in(
    s: AsyncSession,
    keys: Keyring,
    agent_id: uuid.UUID,
    *,
    memory_type: str,
    source: str,
    body: str,
    fingerprint: bytes,
    confidence: float,
    event_date: date | None,
    source_message_id: uuid.UUID | None,
) -> uuid.UUID:
    row_id = uuid.uuid4()
    await s.execute(
        text(
            "INSERT INTO memory_items (id, agent_id, type, source, text_enc, fingerprint, "
            "confidence, event_date, source_message_id) VALUES (:i, :a, "
            "CAST(:ty AS memory_type), CAST(:so AS memory_source), :t, :fp, :c, :d, :sm)"
        ),
        {
            "i": row_id,
            "a": agent_id,
            "ty": memory_type,
            "so": source,
            "t": keys.seal(crypto.MEMORY_TEXT, row_id, body),
            "fp": fingerprint,
            "c": confidence,
            "d": event_date,
            "sm": source_message_id,
        },
    )
    return row_id


async def update_text_in(
    s: AsyncSession,
    keys: Keyring,
    user_id: uuid.UUID,
    item_id: uuid.UUID,
    *,
    body: str,
    fingerprint: bytes,
    confidence: float,
    event_date: date | None,
    expected_version: datetime,
) -> bool:
    """Новый текст живой записи, если она не менялась с чтения (``expected_version``). Отпечаток —
    в том же UPDATE (АР-36); вектор старого текста обнуляет триггер ``clear_stale_embedding``."""
    res = await s.execute(
        text(
            "UPDATE memory_items m SET text_enc = :t, fingerprint = :fp, "
            "confidence = GREATEST(m.confidence, :c), "
            "event_date = COALESCE(:d, m.event_date), updated_at = now() FROM agents a "
            "WHERE m.id = :i AND a.id = m.agent_id AND a.user_id = :u AND m.deleted_at IS NULL "
            "AND m.text_enc IS NOT NULL AND COALESCE(m.updated_at, m.created_at) = :v "
            "RETURNING m.id"
        ),
        {
            "i": item_id,
            "u": user_id,
            "t": keys.seal(crypto.MEMORY_TEXT, item_id, body),
            "fp": fingerprint,
            "c": confidence,
            "d": event_date,
            "v": expected_version,
        },
    )
    return res.one_or_none() is not None


async def raise_confidence_in(
    s: AsyncSession, user_id: uuid.UUID, item_id: uuid.UUID, confidence: float
) -> bool:
    """Повтор того же содержания — уверенность не ниже новой (без смены текста и источника)."""
    res = await s.execute(
        text(
            "UPDATE memory_items m SET confidence = GREATEST(m.confidence, :c) FROM agents a "
            "WHERE m.id = :i AND a.id = m.agent_id AND a.user_id = :u AND m.deleted_at IS NULL "
            "RETURNING m.id"
        ),
        {"i": item_id, "u": user_id, "c": confidence},
    )
    return res.one_or_none() is not None


async def drop_duplicate_in(
    s: AsyncSession, user_id: uuid.UUID, item_id: uuid.UUID, expected_version: datetime
) -> bool:
    """Физически удалить живой дубликат (слияние; содержание осталось в записи-получателе).
    Удалённую владельцем запись (отпечаток) политика не даёт стереть (0009)."""
    res = await s.execute(
        text(
            "DELETE FROM memory_items m USING agents a WHERE m.id = :i AND a.id = m.agent_id "
            "AND a.user_id = :u AND m.deleted_at IS NULL "
            "AND COALESCE(m.updated_at, m.created_at) = :v RETURNING m.id"
        ),
        {"i": item_id, "u": user_id, "v": expected_version},
    )
    return res.one_or_none() is not None


async def set_embedding_in(
    s: AsyncSession, item_id: uuid.UUID, vector: Sequence[float], model: str
) -> bool:
    return bool(
        (
            await s.execute(
                text("SELECT app_set_memory_embedding(:i, :v, :m)"),
                {"i": item_id, "v": to_pgvector(vector), "m": model},
            )
        ).scalar_one()
    )


async def set_profile_summary_in(
    s: AsyncSession, keys: Keyring, user_id: uuid.UUID, agent_id: uuid.UUID, body: str
) -> bool:
    res = await s.execute(
        text(
            "UPDATE agents SET profile_summary_enc = :t WHERE id = :a AND user_id = :u RETURNING id"
        ),
        {"a": agent_id, "u": user_id, "t": keys.seal(crypto.AGENT_PROFILE_SUMMARY, agent_id, body)},
    )
    return res.one_or_none() is not None


async def conversation_summaries_in(
    s: AsyncSession, keys: Keyring, user_id: uuid.UUID
) -> list[tuple[uuid.UUID, str]]:
    """Сырые (с конвертом) сводки диалогов владельца — для пересборки без удалённого (АР-31)."""
    rows = await s.execute(
        text(
            "SELECT c.id, c.summary_enc FROM conversations c JOIN agents a ON a.id = c.agent_id "
            "WHERE a.user_id = :u AND c.summary_enc IS NOT NULL ORDER BY c.started_at, c.id"
        ),
        {"u": user_id},
    )
    out = []
    for r in rows:
        cid = _uuid(r.id)
        raw, _ = open_row(keys, crypto.CONVERSATION_SUMMARY, cid, r.summary_enc)
        if raw is not None:
            out.append((cid, raw))
    return out


async def replace_summary_in(
    s: AsyncSession,
    keys: Keyring,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    expected_raw: str,
    new_raw: str | None,
) -> bool:
    """Заменить сводку диалога, если она всё ещё ``expected_raw`` (``FOR UPDATE`` + сверка, как
    ``chat.save_history_summary``; ревью 2.4 MINOR-5): иначе её уже переписали — пропуск.
    ``new_raw = None`` — сводки нет."""
    row = (
        await s.execute(
            text(
                "SELECT c.summary_enc FROM conversations c JOIN agents a ON a.id = c.agent_id "
                "WHERE c.id = :c AND a.user_id = :u FOR UPDATE OF c"
            ),
            {"c": conversation_id, "u": user_id},
        )
    ).one_or_none()
    if row is None or row.summary_enc is None:
        return False
    current, _ = open_row(keys, crypto.CONVERSATION_SUMMARY, conversation_id, row.summary_enc)
    if current != expected_raw:
        return False
    await s.execute(
        text("UPDATE conversations SET summary_enc = :t WHERE id = :c"),
        {
            "c": conversation_id,
            "t": None
            if new_raw is None
            else keys.seal(crypto.CONVERSATION_SUMMARY, conversation_id, new_raw),
        },
    )
    return True


async def take_for_forget_in(
    s: AsyncSession, keys: Keyring, user_id: uuid.UUID, item_id: uuid.UUID
) -> tuple[uuid.UUID, str] | None:
    """Живая своя запись под блокировкой: (агент, расшифрованный текст) — до удаления (АР-37:
    текст нужен задаче очистки сводок и живёт только в памяти процесса). ``None`` — нет такой."""
    row = (
        await s.execute(
            text(
                "SELECT m.agent_id, m.text_enc FROM memory_items m JOIN agents a "
                "ON a.id = m.agent_id WHERE m.id = :i AND a.user_id = :u "
                "AND m.deleted_at IS NULL AND m.text_enc IS NOT NULL FOR UPDATE OF m"
            ),
            {"i": item_id, "u": user_id},
        )
    ).one_or_none()
    if row is None:
        return None
    body, _ = open_row(keys, crypto.MEMORY_TEXT, item_id, row.text_enc)
    return _uuid(row.agent_id), body or ""


async def delete_memory_in(s: AsyncSession, item_id: uuid.UUID) -> bool:
    return bool(
        (await s.execute(text("SELECT app_delete_memory(:i)"), {"i": item_id})).scalar_one()
    )


async def clear_profile_summary_in(s: AsyncSession, user_id: uuid.UUID) -> None:
    """АР-37 (1): сводка профиля обнуляется сразу при удалении записи памяти."""
    await s.execute(
        text("UPDATE agents SET profile_summary_enc = NULL WHERE user_id = :u"), {"u": user_id}
    )


async def extraction_done_in(
    s: AsyncSession,
    token: uuid.UUID,
    session_through: uuid.UUID | None,
    partner_through: uuid.UUID | None,
) -> bool:
    return bool(
        (
            await s.execute(
                text("SELECT app_memory_extraction_done(:t, :s, :p)"),
                {"t": token, "s": session_through, "p": partner_through},
            )
        ).scalar_one()
    )


async def compaction_done_in(s: AsyncSession, token: uuid.UUID) -> bool:
    return bool(
        (await s.execute(text("SELECT app_memory_compaction_done(:t)"), {"t": token})).scalar_one()
    )

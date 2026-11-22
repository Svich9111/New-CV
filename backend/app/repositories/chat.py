"""Слой доступа личного чата и сборки контекста агента (ФТ-05; ТЗ §6, §6.1, §9.2; задача 2.1).

Каждая функция — внутри ``user_scope`` (роль ``feelings_app``, RLS) **и** с явным отбором по
владельцу (``agents.user_id = :u``, ``couple_members.user_id = :u``): изоляция держится базой,
а не промптом и не одной проверкой в коде (ТЗ §6, §9.2 п.1). Данных партнёра (его агента,
памяти, чата, профиля) здесь нет и прочитать их RLS не даст; из пары — только доставленные
сообщения текущей активной пары, как их видел владелец: свои — оригинал, полученные — версия
для получателя (``app_couple_feed``, АР-11).

Тексты расшифровываются построчно (``texts.open_row``): строка, которая не расшифровалась, в
контекст не идёт (считается в ``unavailable``), сборка не падает. В журналы — ни текстов, ни id.

Сводка старых реплик (``conversations.summary_enc``) хранится зашифрованной в конверте JSON
``{"v": 1, "text", "through_at", "through_id"}``: отметка — последняя реплика, вошедшая в
сводку; реплики до неё в контекст не идут (миграция не нужна). Старая сводка без конверта
читается как текст без отметки."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import crypto
from app.core.crypto import Keyring
from app.db import user_scope
from app.repositories.texts import Author, ConversationText, open_row

# Статусы сообщения пары, которые владелец видел как доставленные (ТЗ §6.2 п.7, АР-25 п.1).
DELIVERED = ("delivered", "read", "answered")
FEED_MAX = 100  # app_couple_feed отдаёт не больше 100 строк
SUMMARY_FORMAT = 1

# crisis.CrisisType → значение перечисления safety_event_type (миграция 0001).
SAFETY_EVENT_TYPES: dict[str, str] = {
    "violence": "violence",
    "outgoing_threat": "threat_outgoing",
    "self_harm": "self_harm",
    "life_threat": "life_threat",
    "underage": "underage",
}


class OwnerGone(Exception):
    """Владельца нет, он удалён или у него нет агента (онбординг не пройден). Для воркеров —
    штатный исход (Б-44): задание завершается без повтора; для API — 401/403."""


# --------------------------------------------------------------------------- сводка истории


@dataclass(frozen=True)
class HistorySummary:
    """Сводка реплик до отметки ``through`` включительно (``None`` — отметки нет)."""

    text: str
    through_at: datetime | None = None
    through_id: uuid.UUID | None = None


def pack_summary(summary: HistorySummary) -> str:
    return json.dumps(
        {
            "v": SUMMARY_FORMAT,
            "text": summary.text,
            "through_at": summary.through_at.isoformat() if summary.through_at else None,
            "through_id": str(summary.through_id) if summary.through_id else None,
        },
        ensure_ascii=False,
    )


def unpack_summary(raw: str) -> HistorySummary:
    """Конверт → сводка; любой другой текст — сводка без отметки (записана до 2.1)."""
    try:
        data = json.loads(raw)
    except ValueError:
        return HistorySummary(raw)
    if not (isinstance(data, dict) and data.get("v") == SUMMARY_FORMAT):
        return HistorySummary(raw)
    body = data.get("text")
    at, rid = data.get("through_at"), data.get("through_id")
    try:
        through_at = datetime.fromisoformat(at) if isinstance(at, str) else None
        through_id = uuid.UUID(rid) if isinstance(rid, str) else None
    except ValueError:
        through_at, through_id = None, None
    if through_at is None or through_id is None:
        through_at, through_id = None, None
    return HistorySummary(body if isinstance(body, str) else "", through_at, through_id)


def summary_text(raw: str) -> str:
    """Текст сводки для людей (выгрузка ФТ-21): без служебной отметки."""
    return unpack_summary(raw).text


# --------------------------------------------------------------------------- строки контекста


@dataclass(frozen=True)
class MemoryRow:
    id: uuid.UUID
    type: str
    text: str
    found_by: Literal["vector", "recency"]


@dataclass(frozen=True)
class PairRow:
    """Одобренное и доставленное сообщение текущей пары — как его видел владелец."""

    id: uuid.UUID
    outgoing: bool
    text: str
    sent_at: datetime


@dataclass(frozen=True)
class ContextRows:
    owner_name: str
    partner_name: str
    profile_summary: str | None
    summary: HistorySummary | None
    turns: list[ConversationText]  # реплики после отметки сводки, по порядку
    memory: list[MemoryRow]  # лучшие — первыми
    pair: list[PairRow]  # новые — первыми
    unavailable: dict[str, int] = field(default_factory=dict)  # блок → сколько не расшифровано


def _uuid(value: object) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


async def _owner(s: AsyncSession, user_id: uuid.UUID) -> tuple[str, str, uuid.UUID, bytes | None]:
    row = (
        await s.execute(
            text(
                "SELECT u.name, u.address_form, u.partner_name, a.id AS agent_id, "
                "a.profile_summary_enc FROM users u JOIN agents a ON a.user_id = u.id "
                "WHERE u.id = :u AND u.deleted_at IS NULL"
            ),
            {"u": user_id},
        )
    ).one_or_none()
    if row is None:
        raise OwnerGone
    owner = (row.address_form or row.name or "").strip()
    return owner, (row.partner_name or "").strip(), _uuid(row.agent_id), row.profile_summary_enc


async def _summary(
    s: AsyncSession, keys: Keyring, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> tuple[bool, HistorySummary | None, bool]:
    """(диалог свой, сводка, сводка не расшифровалась)."""
    row = (
        await s.execute(
            text(
                "SELECT c.summary_enc FROM conversations c JOIN agents a ON a.id = c.agent_id "
                "JOIN users u ON u.id = a.user_id "
                "WHERE c.id = :c AND a.user_id = :u AND u.deleted_at IS NULL"
            ),
            {"c": conversation_id, "u": user_id},
        )
    ).one_or_none()
    if row is None:
        return False, None, False
    if row.summary_enc is None:
        return True, None, False
    raw, reason = open_row(keys, crypto.CONVERSATION_SUMMARY, conversation_id, row.summary_enc)
    if raw is None:
        # Сводка не читается: отметку не знаем, поэтому берём все реплики (бюджет их усечёт).
        return True, None, reason is not None
    return True, unpack_summary(raw), False


async def _turns_after(
    s: AsyncSession,
    keys: Keyring,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    summary: HistorySummary | None,
    limit: int | None,
) -> tuple[list[ConversationText], int]:
    params: dict[str, object] = {"c": conversation_id, "u": user_id}
    after = ""
    if summary is not None and summary.through_at is not None:
        after = "AND (m.created_at, m.id) > (:ta, :ti) "
        params |= {"ta": summary.through_at, "ti": summary.through_id}
    tail = ""
    if limit is not None:
        tail = "LIMIT :n"
        params["n"] = limit
    rows = await s.execute(
        text(
            "SELECT m.id, m.author::text AS author, m.text_enc, m.created_at "  # noqa: S608 — фрагменты-константы, ввод — параметры
            "FROM conversation_messages m JOIN conversations c ON c.id = m.conversation_id "
            "JOIN agents a ON a.id = c.agent_id "
            f"WHERE m.conversation_id = :c AND a.user_id = :u {after}"
            f"ORDER BY m.created_at DESC, m.id DESC {tail}"
        ),
        params,
    )
    turns: list[ConversationText] = []
    lost = 0
    for r in rows:
        row_id = _uuid(r.id)
        body, reason = open_row(keys, crypto.CONVERSATION_MESSAGE_TEXT, row_id, r.text_enc)
        if body is None:
            lost += 1
        turns.append(ConversationText(row_id, r.author, body, r.created_at, reason))
    turns.reverse()
    return turns, lost


def parse_vector(raw: object) -> list[float] | None:
    """Вектор из текстового вида pgvector ``[x1,x2,...]`` (так его отдаёт драйвер без кодека)."""
    if isinstance(raw, list | tuple):
        return [float(v) for v in raw]
    if not isinstance(raw, str) or not raw.startswith("[") or not raw.endswith("]"):
        return None
    try:
        return [float(v) for v in raw[1:-1].split(",") if v]
    except ValueError:
        return None


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


async def _memory(
    s: AsyncSession,
    keys: Keyring,
    user_id: uuid.UUID,
    agent_id: uuid.UUID,
    query_vector: Sequence[float] | None,
    limit: int,
    scan_limit: int,
) -> tuple[list[MemoryRow], int]:
    """По смыслу — косинусная близость к вектору реплики среди записей с вектором (не больше
    ``scan_limit`` самых свежих); недобор — по свежести. Удалённые записи (ФТ-13:
    ``deleted_at``, текст и вектор стёрты) не берутся никогда.

    Близость считается в приложении: у ``feelings_app`` нет EXECUTE на функции pgvector
    (``REVOKE ALL ON ALL FUNCTIONS … FROM PUBLIC``, миграция 0001), поэтому ни ``<=>``, ни
    ``CAST(… AS vector)`` в запросах приложения не работают. Перенос в БД (индекс, ``<=>``) —
    после миграции A1 (отчёт 2.1). Читаются только векторы, тексты — только выбранных."""
    base = (
        "FROM memory_items m JOIN agents a ON a.id = m.agent_id "
        "WHERE m.agent_id = :a AND a.user_id = :u AND m.deleted_at IS NULL "
        "AND m.text_enc IS NOT NULL "
    )
    chosen: list[tuple[uuid.UUID, Literal["vector", "recency"]]] = []
    if query_vector is not None and limit > 0:
        rows = await s.execute(
            text(
                "SELECT m.id, m.embedding " + base + "AND m.embedding IS NOT NULL "
                "ORDER BY COALESCE(m.updated_at, m.created_at) DESC, m.id LIMIT :n"
            ),
            {"a": agent_id, "u": user_id, "n": scan_limit},
        )
        scored = []
        for r in rows:
            vec = parse_vector(r.embedding)
            if vec is not None:
                scored.append((-_cosine(query_vector, vec), str(r.id), _uuid(r.id)))
        scored.sort()
        chosen += [(rid, "vector") for _, _, rid in scored[:limit]]
    rest = limit - len(chosen)
    if rest > 0:
        rows = await s.execute(
            text(
                "SELECT m.id " + base + "AND NOT (m.id = ANY(CAST(:seen AS uuid[]))) "
                "ORDER BY COALESCE(m.updated_at, m.created_at) DESC, m.id LIMIT :n"
            ),
            {"a": agent_id, "u": user_id, "seen": [rid for rid, _ in chosen], "n": rest},
        )
        chosen += [(_uuid(r.id), "recency") for r in rows]
    if not chosen:
        return [], 0
    texts_by_id = {
        _uuid(r.id): (r.type, r.text_enc)
        for r in await s.execute(
            text(
                "SELECT m.id, m.type::text AS type, m.text_enc "
                + base
                + "AND m.id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"a": agent_id, "u": user_id, "ids": [rid for rid, _ in chosen]},
        )
    }
    out: list[MemoryRow] = []
    lost = 0
    for row_id, how in chosen:
        mtype, blob = texts_by_id[row_id]
        body, _ = open_row(keys, crypto.MEMORY_TEXT, row_id, blob)
        if body is None:
            lost += 1
            continue
        out.append(MemoryRow(row_id, mtype, body, how))
    return out, lost


async def _pair(
    s: AsyncSession, keys: Keyring, user_id: uuid.UUID, limit: int
) -> tuple[list[PairRow], int]:
    """Доставленные сообщения **текущей активной** пары владельца: распавшаяся пара (бывший
    партнёр) и чужие пары в контекст не идут. Свои — оригинал (контекст черновика, Б-38),
    полученные — версия для получателя (то, что владелец видел, ФТ-09)."""
    couple = (
        await s.execute(
            text(
                "SELECT cm.couple_id FROM couple_members cm JOIN couples c ON c.id = cm.couple_id "
                "WHERE cm.user_id = :u AND cm.active AND c.status = 'active'"
            ),
            {"u": user_id},
        )
    ).scalar_one_or_none()
    if couple is None or limit <= 0:
        return [], 0
    rows = await s.execute(
        text(
            "SELECT f.id, f.outgoing, f.text_enc, f.sent_at, pm.draft_id "
            "FROM app_couple_feed(:l, NULL) f JOIN partner_messages pm ON pm.id = f.id "
            "WHERE f.couple_id = :cc AND f.status::text = ANY(CAST(:st AS text[])) "
            "AND (pm.author_id = :u OR pm.recipient_id = :u) "
            "ORDER BY f.sent_at DESC, f.id LIMIT :n"
        ),
        {"l": FEED_MAX, "cc": couple, "st": list(DELIVERED), "u": user_id, "n": limit},
    )
    out: list[PairRow] = []
    lost = 0
    for r in rows:
        msg_id = _uuid(r.id)
        body: str | None = None
        if r.text_enc is not None:
            if r.outgoing:
                draft = None if r.draft_id is None else _uuid(r.draft_id)
                body, _ = open_row(keys, crypto.MESSAGE_ORIGINAL, draft, r.text_enc)
            else:
                body, _ = open_row(keys, crypto.MESSAGE_ADAPTED, msg_id, r.text_enc)
        if body is None:
            lost += 1
            continue
        out.append(PairRow(msg_id, bool(r.outgoing), body, r.sent_at))
    return out, lost


async def read_context(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    query_vector: Sequence[float] | None = None,
    memory_limit: int = 8,
    memory_scan_limit: int = 500,
    turns_limit: int = 40,
    pair_limit: int = 20,
) -> ContextRows:
    """Всё для контекста агента владельца — одной транзакцией (один снимок). ``OwnerGone`` —
    владельца нет или нет агента; ``LookupError`` — диалог не свой."""
    async with user_scope(sm, user_id) as s:
        owner, partner, agent_id, profile_blob = await _owner(s, user_id)
        own, summary, summary_lost = await _summary(s, keys, user_id, conversation_id)
        if not own:
            raise LookupError("диалог не найден среди своих")
        turns, turns_lost = await _turns_after(
            s, keys, user_id, conversation_id, summary, turns_limit
        )
        memory, memory_lost = await _memory(
            s, keys, user_id, agent_id, query_vector, memory_limit, memory_scan_limit
        )
        pair, pair_lost = await _pair(s, keys, user_id, pair_limit)
    profile, profile_reason = (
        (None, None)
        if profile_blob is None
        else open_row(keys, crypto.AGENT_PROFILE_SUMMARY, agent_id, profile_blob)
    )
    unavailable = {
        k: v
        for k, v in (
            ("profile", int(profile_reason is not None)),
            ("summary", int(summary_lost)),
            ("dialog", turns_lost),
            ("memory", memory_lost),
            ("pair_messages", pair_lost),
        )
        if v
    }
    return ContextRows(
        owner_name=owner,
        partner_name=partner,
        profile_summary=profile,
        summary=summary,
        turns=turns,
        memory=memory,
        pair=pair,
        unavailable=unavailable,
    )


# --------------------------------------------------------------------------- личный чат


async def current_conversation(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> uuid.UUID:
    """Текущий диалог владельца (последний по началу); нет — создаётся. ``OwnerGone`` — нет
    владельца или агента. Сессии пока не делятся по времени (одна лента; см. отчёт 2.1)."""
    async with user_scope(sm, user_id) as s:
        agent = (
            await s.execute(
                text(
                    "SELECT a.id FROM agents a JOIN users u ON u.id = a.user_id "
                    "WHERE a.user_id = :u AND u.deleted_at IS NULL"
                ),
                {"u": user_id},
            )
        ).scalar_one_or_none()
        if agent is None:
            raise OwnerGone
        conv = (
            await s.execute(
                text(
                    "SELECT id FROM conversations WHERE agent_id = :a "
                    "ORDER BY started_at DESC, id DESC LIMIT 1"
                ),
                {"a": agent},
            )
        ).scalar_one_or_none()
        if conv is None:
            conv = (
                await s.execute(
                    text("INSERT INTO conversations (agent_id) VALUES (:a) RETURNING id"),
                    {"a": agent},
                )
            ).scalar_one()
    return _uuid(conv)


async def add_message(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    author: Author,
    body: str,
) -> uuid.UUID | None:
    """Реплика в свой диалог (шифротекст). ``None`` — диалог не свой или владелец удалён
    (Б-44: не ошибка). Реплику владельца API пишет **до** вызова модели (ТЗ §10)."""
    if author not in ("owner", "agent"):
        raise ValueError("author")
    row_id = uuid.uuid4()
    async with user_scope(sm, user_id) as s:
        res = await s.execute(
            text(
                "INSERT INTO conversation_messages (id, conversation_id, author, text_enc) "
                "SELECT :i, c.id, CAST(:au AS message_author), :t FROM conversations c "
                "JOIN agents a ON a.id = c.agent_id JOIN users u ON u.id = a.user_id "
                "WHERE c.id = :c AND a.user_id = :u AND u.deleted_at IS NULL RETURNING id"
            ),
            {
                "i": row_id,
                "c": conversation_id,
                "u": user_id,
                "au": author,
                "t": keys.seal(crypto.CONVERSATION_MESSAGE_TEXT, row_id, body),
            },
        )
        if res.one_or_none() is None:
            return None
    return row_id


async def read_history(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> tuple[HistorySummary | None, list[ConversationText]]:
    """Сводка и **все** реплики после её отметки (для сжатия). ``LookupError`` — не свой."""
    async with user_scope(sm, user_id) as s:
        own, summary, _ = await _summary(s, keys, user_id, conversation_id)
        if not own:
            raise LookupError("диалог не найден среди своих")
        turns, _ = await _turns_after(s, keys, user_id, conversation_id, summary, None)
    return summary, turns


async def save_history_summary(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    summary: HistorySummary,
    *,
    expected_through_id: uuid.UUID | None,
) -> bool:
    """Записать сводку, если отметка в БД всё ещё ``expected_through_id`` (иначе сводку уже
    обновил другой запрос — False, ничего не меняется). False и при чужом диалоге или удалённом
    владельце (Б-44). Шифрование — ``conversations|summary_enc|<id диалога>``."""
    if summary.through_at is None or summary.through_id is None:
        raise ValueError("у сводки нет отметки")
    async with user_scope(sm, user_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT c.summary_enc FROM conversations c JOIN agents a ON a.id = c.agent_id "
                    "JOIN users u ON u.id = a.user_id "
                    "WHERE c.id = :c AND a.user_id = :u AND u.deleted_at IS NULL FOR UPDATE OF c"
                ),
                {"c": conversation_id, "u": user_id},
            )
        ).one_or_none()
        if row is None:
            return False
        current: uuid.UUID | None = None
        if row.summary_enc is not None:
            raw, _ = open_row(keys, crypto.CONVERSATION_SUMMARY, conversation_id, row.summary_enc)
            current = unpack_summary(raw).through_id if raw is not None else None
        if current != expected_through_id:
            return False
        await s.execute(
            text("UPDATE conversations SET summary_enc = :t WHERE id = :c"),
            {
                "c": conversation_id,
                "t": keys.seal(crypto.CONVERSATION_SUMMARY, conversation_id, pack_summary(summary)),
            },
        )
    return True


# --------------------------------------------------------------------------- память и события


async def record_safety_events(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID, types: Iterable[str]
) -> int:
    """События кризисного протокола — только тип, без текста (ТЗ §9.3). ``types`` — строки
    ``crisis.CrisisType``; неизвестные пропускаются. Партнёру ничего не сообщается."""
    values = list(dict.fromkeys(SAFETY_EVENT_TYPES[t] for t in types if t in SAFETY_EVENT_TYPES))
    if not values:
        return 0
    async with user_scope(sm, user_id) as s:
        for value in values:
            await s.execute(
                text(
                    "INSERT INTO safety_events (user_id, type) "
                    "VALUES (:u, CAST(:t AS safety_event_type))"
                ),
                {"u": user_id, "t": value},
            )
    return len(values)

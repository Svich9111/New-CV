"""Слой доступа личного чата и сборки контекста агента (ФТ-05; ТЗ §6, §6.1, §9.2; задача 2.1).

Каждая функция — внутри ``user_scope`` (роль ``feelings_app``, RLS) **и** с явным отбором по
владельцу (``agents.user_id = :u``, ``couple_members.user_id = :u``): изоляция держится базой,
а не промптом и не одной проверкой в коде (ТЗ §6, §9.2 п.1). Данных партнёра (его агента,
памяти, чата, профиля) здесь нет и прочитать их RLS не даст; из пары — только доставленные
сообщения текущей активной пары, как их видел владелец: свои — оригинал, полученные — версия
для получателя (как в ``app_couple_feed``, АР-11; см. ``_pair``).

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
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import to_pgvector
from app.core import crypto
from app.core.crypto import Keyring
from app.db import sqlstate, user_scope
from app.repositories.texts import Author, ConversationText, open_row

# Статусы сообщения пары, которые владелец видел как доставленные (ТЗ §6.2 п.7, АР-25 п.1).
DELIVERED = ("delivered", "read", "answered")
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
    # АР-37: id удалённых владельцем записей памяти, которые ещё не убраны из сводки. Пока
    # список не пуст, сводка «на очистку»: в контекст, выгрузку и сжатие чата не идёт.
    forget: tuple[str, ...] = ()

    @property
    def visible_text(self) -> str:
        """Текст, который можно показывать агенту и человеку (на очистке — пусто)."""
        return "" if self.forget else self.text


def pack_summary(summary: HistorySummary) -> str:
    data: dict[str, object] = {
        "v": SUMMARY_FORMAT,
        "text": summary.text,
        "through_at": summary.through_at.isoformat() if summary.through_at else None,
        "through_id": str(summary.through_id) if summary.through_id else None,
    }
    if summary.forget:
        data["forget"] = list(summary.forget)
    return json.dumps(data, ensure_ascii=False)


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
    forget = data.get("forget")
    pending = tuple(str(x) for x in forget) if isinstance(forget, list) else ()
    return HistorySummary(body if isinstance(body, str) else "", through_at, through_id, pending)


def summary_text(raw: str) -> str:
    """Текст сводки для людей (выгрузка ФТ-21): без служебной отметки; сводка на очистке после
    удаления записи памяти (АР-37) — пусто."""
    return unpack_summary(raw).visible_text


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
    # БД отклонила вектор или модель запроса (22023): память — по свежести (Б-64).
    vector_rejected: bool = False


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


TURN_SCAN_FACTOR = (
    10  # сколько строк на одну нужную реплику можно просмотреть, пропуская нечитаемые
)


def _readable(t: ConversationText) -> bool:
    return t.text is not None and bool(t.text.strip())


async def _turns_after(
    s: AsyncSession,
    keys: Keyring,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    summary: HistorySummary | None,
    limit: int | None,
    upto: FeedPosition | None = None,
) -> tuple[list[ConversationText], int]:
    """Реплики после отметки сводки, по порядку. ``limit`` — сколько **читаемых** (расшифрованных
    и непустых) реплик нужно с конца: нечитаемые строки места в бюджете не занимают и предел не
    съедают (тест A8-3) — чтение идёт страницами дальше назад, но не больше
    ``TURN_SCAN_FACTOR × limit`` строк (потерянный ключ, 4.7). → (реплики, сколько нечитаемых;
    если просмотр упёрся в потолок, а строки ещё есть, — +1 к потерям, сборка пишет её в
    ``unavailable``). ``upto`` — последняя реплика, которая войдёт (ответ на неё по повтору с
    ключом, АР-51): более поздние реплики не берутся."""
    params: dict[str, object] = {"c": conversation_id, "u": user_id}
    after = ""
    if summary is not None and summary.through_at is not None:
        after = "AND (m.created_at, m.id) > (:ta, :ti) "
        params |= {"ta": summary.through_at, "ti": summary.through_id}
    if upto is not None:
        after += "AND (m.created_at, m.id) <= (:ua, :ui) "
        params |= {"ua": upto.created_at, "ui": upto.id}
    sql = (
        "SELECT m.id, m.author::text AS author, m.text_enc, m.created_at "  # noqa: S608 — фрагменты-константы, ввод — параметры
        "FROM conversation_messages m JOIN conversations c ON c.id = m.conversation_id "
        "JOIN agents a ON a.id = c.agent_id "
        f"WHERE m.conversation_id = :c AND a.user_id = :u {after}"
        "{before}ORDER BY m.created_at DESC, m.id DESC {tail}"
    )
    turns: list[ConversationText] = []
    lost = 0
    readable = 0
    scanned = 0
    cursor: tuple[datetime, uuid.UUID] | None = None
    while True:
        page_params = dict(params)
        before = ""
        if cursor is not None:
            before = "AND (m.created_at, m.id) < (:ba, :bi) "
            page_params |= {"ba": cursor[0], "bi": cursor[1]}
        tail = ""
        page = None
        if limit is not None:
            page = max(limit - readable, 1)
            tail = "LIMIT :n"
            page_params["n"] = page
        rows = (await s.execute(text(sql.format(before=before, tail=tail)), page_params)).all()
        for r in rows:
            row_id = _uuid(r.id)
            body, reason = open_row(keys, crypto.CONVERSATION_MESSAGE_TEXT, row_id, r.text_enc)
            if body is None:
                lost += 1
            item = ConversationText(row_id, r.author, body, r.created_at, reason)
            readable += int(_readable(item))
            turns.append(item)
            cursor = (r.created_at, row_id)
        scanned += len(rows)
        if limit is None or page is None or len(rows) < page or readable >= limit:
            break
        if scanned >= TURN_SCAN_FACTOR * max(limit, 1):
            lost += 1  # дальше не смотрим: старее — только в потерях, не молча
            break
    turns.reverse()
    return turns, lost


async def _turns_head(
    s: AsyncSession,
    keys: Keyring,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    summary: HistorySummary | None,
    limit: int,
) -> tuple[int, list[ConversationText]]:
    """(сколько всего реплик после отметки, первые ``limit`` из них по порядку) — расшифровываются
    только первые (раунд сжатия, R2-MINOR-3)."""
    params: dict[str, object] = {"c": conversation_id, "u": user_id, "n": limit}
    after = ""
    if summary is not None and summary.through_at is not None:
        after = "AND (m.created_at, m.id) > (:ta, :ti) "
        params |= {"ta": summary.through_at, "ti": summary.through_id}
    base = (
        "FROM conversation_messages m JOIN conversations c ON c.id = m.conversation_id "
        "JOIN agents a ON a.id = c.agent_id "
        f"WHERE m.conversation_id = :c AND a.user_id = :u {after}"
    )
    total = (await s.execute(text("SELECT count(*) " + base), params)).scalar_one()
    rows = await s.execute(
        text(
            "SELECT m.id, m.author::text AS author, m.text_enc, m.created_at "
            + base
            + "ORDER BY m.created_at, m.id LIMIT :n"
        ),
        params,
    )
    head = []
    for r in rows:
        row_id = _uuid(r.id)
        body, reason = open_row(keys, crypto.CONVERSATION_MESSAGE_TEXT, row_id, r.text_enc)
        head.append(ConversationText(row_id, r.author, body, r.created_at, reason))
    return int(total), head


async def _memory(
    s: AsyncSession,
    keys: Keyring,
    user_id: uuid.UUID,
    agent_id: uuid.UUID,
    query_vector: Sequence[float] | None,
    limit: int,
    model: str | None = None,
    rejected: list[bool] | None = None,
) -> tuple[list[MemoryRow], int]:
    """По смыслу — функцией БД ``app_memory_nearest`` (миграция 0008, АР-35; индекс HNSW): только
    своя память (``app_uid()``), не удалённые записи с текстом и с вектором модели ``model``
    (``Embedder.model``); вектор передаётся текстом, прямого доступа к pgvector у приложения нет.
    Недобор — по свежести (и весь блок по свежести, если вектора или модели нет). Удалённые
    записи (ФТ-13: ``deleted_at``, текст, вектор и модель стёрты) не берутся никогда."""
    base = (
        "FROM memory_items m JOIN agents a ON a.id = m.agent_id "
        "WHERE m.agent_id = :a AND a.user_id = :u AND m.deleted_at IS NULL "
        "AND m.text_enc IS NOT NULL "
    )
    found: list[tuple[uuid.UUID, str, bytes, Literal["vector", "recency"]]] = []
    if query_vector is not None and model and limit > 0:
        # Точка сохранения (Б-64): отказ функции (22023 — вектор или модель не подошли) не
        # обрывает транзакцию сборки — память берётся по свежести.
        try:
            async with s.begin_nested():
                hits = (
                    await s.execute(
                        text(
                            "SELECT id, type::text AS type, text_enc "
                            "FROM app_memory_nearest(:q, :m, :n)"
                        ),
                        {"q": to_pgvector(query_vector), "m": model, "n": limit},
                    )
                ).all()
        except DBAPIError as exc:
            if sqlstate(exc) != "22023":
                raise
            hits = []
            if rejected is not None:
                rejected.append(True)
        found += [(_uuid(r.id), r.type, r.text_enc, "vector") for r in hits][:limit]
    rest = limit - len(found)
    if rest > 0:
        rows = await s.execute(
            text(
                "SELECT m.id, m.type::text AS type, m.text_enc "
                + base
                + "AND NOT (m.id = ANY(CAST(:seen AS uuid[]))) "
                "ORDER BY COALESCE(m.updated_at, m.created_at) DESC, m.id LIMIT :n"
            ),
            {"a": agent_id, "u": user_id, "seen": [f[0] for f in found], "n": rest},
        )
        found += [(_uuid(r.id), r.type, r.text_enc, "recency") for r in rows]
    out: list[MemoryRow] = []
    lost = 0
    for row_id, mtype, blob, how in found:
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
    # Фильтр по паре, статусу и стороне — в самом запросе, до LIMIT (ревью MINOR-11, тест О-3):
    # остановленные и ждущие конвейера сообщения не вытесняют доставленные. Своё — оригинал: это
    # шифротекст своего отправленного черновика (``app_send_draft`` копирует его байты в
    # ``original_enc``; отправленный черновик не меняется и не удаляется, 0005), полученное —
    # версия для получателя (``adapted_enc``). ``original_enc`` приложению по-прежнему не нужен
    # (АР-11); черновик партнёра RLS не отдаёт (и соединение — только для своих).
    rows = await s.execute(
        text(
            "SELECT pm.id, pm.author_id = :u AS outgoing, pm.sent_at, pm.draft_id, "
            "CASE WHEN pm.author_id = :u THEN md.text_enc ELSE pm.adapted_enc END AS text_enc "
            "FROM partner_messages pm "
            "LEFT JOIN message_drafts md ON md.id = pm.draft_id AND pm.author_id = :u "
            "WHERE pm.couple_id = :cc AND pm.status::text = ANY(CAST(:st AS text[])) "
            "AND (pm.author_id = :u OR pm.recipient_id = :u) "
            "ORDER BY pm.sent_at DESC, pm.id LIMIT :n"
        ),
        {"cc": couple, "st": list(DELIVERED), "u": user_id, "n": limit},
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
    query_model: str | None = None,
    memory_limit: int = 8,
    turns_limit: int | None = None,
    pair_limit: int = 20,
    upto: FeedPosition | None = None,
) -> ContextRows:
    """Всё для контекста агента владельца — одной транзакцией (один снимок). ``OwnerGone`` —
    владельца нет или нет агента; ``LookupError`` — диалог не свой. ``upto`` — реплики диалога
    не позже этой (см. ``_turns_after``)."""
    async with user_scope(sm, user_id) as s:
        owner, partner, agent_id, profile_blob = await _owner(s, user_id)
        own, summary, summary_lost = await _summary(s, keys, user_id, conversation_id)
        if not own:
            raise LookupError("диалог не найден среди своих")
        turns, turns_lost = await _turns_after(
            s, keys, user_id, conversation_id, summary, turns_limit, upto
        )
        rejected: list[bool] = []
        memory, memory_lost = await _memory(
            s, keys, user_id, agent_id, query_vector, memory_limit, query_model, rejected
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
        vector_rejected=bool(rejected),
    )


# --------------------------------------------------------------------------- личный чат


async def current_conversation(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> uuid.UUID:
    """Текущий диалог владельца (последний по началу); нет — создаётся. ``OwnerGone`` — нет
    владельца или агента. Сессии пока не делятся по времени (одна лента; см. отчёт 2.1).

    Строка агента блокируется (``FOR NO KEY UPDATE``) до проверки и вставки: два одновременных
    первых запроса не создают два диалога (ревью MINOR-7) — второй ждёт и видит диалог первого.
    Миграция не нужна; уникальность «один диалог на агента» в схеме не вводится — несколько
    сессий на агента допускает §7 (деление на сессии — 2.4)."""
    async with user_scope(sm, user_id) as s:
        return await _conversation(s, user_id)


async def _conversation(s: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
    """Тело ``current_conversation`` внутри уже открытой транзакции владельца."""
    agent = (
        await s.execute(
            text(
                "SELECT a.id FROM agents a JOIN users u ON u.id = a.user_id "
                "WHERE a.user_id = :u AND u.deleted_at IS NULL FOR NO KEY UPDATE OF a"
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
    *,
    reply_to: uuid.UUID | None = None,
    crisis: bool = False,
) -> uuid.UUID | None:
    """Реплика в свой диалог (шифротекст). ``None`` — диалог не свой или владелец удалён
    (Б-44: не ошибка). Реплику владельца API пишет **до** вызова модели (ТЗ §10). У ответа
    агента — ``reply_to`` (реплика владельца того же диалога; второй ответ на неё — 23505,
    индекс 0018) и ``crisis`` — ответ кризисного протокола (АР-51)."""
    async with user_scope(sm, user_id) as s:
        return await _insert_message(
            s, keys, user_id, conversation_id, author, body, reply_to=reply_to, crisis=crisis
        )


async def _insert_message(
    s: AsyncSession,
    keys: Keyring,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    author: Author,
    body: str,
    *,
    reply_to: uuid.UUID | None = None,
    crisis: bool = False,
    client_message_id: uuid.UUID | None = None,
    lease_s: float | None = None,
) -> uuid.UUID | None:
    return (
        await _insert_row(
            s,
            keys,
            user_id,
            conversation_id,
            author,
            body,
            reply_to=reply_to,
            crisis=crisis,
            client_message_id=client_message_id,
            lease_s=lease_s,
        )
    )[0]


async def _insert_row(
    s: AsyncSession,
    keys: Keyring,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    author: Author,
    body: str,
    *,
    reply_to: uuid.UUID | None = None,
    crisis: bool = False,
    client_message_id: uuid.UUID | None = None,
    lease_s: float | None = None,
) -> tuple[uuid.UUID | None, datetime | None, datetime | None]:
    """→ (id, время создания, аренда ответа до) — ``(None, None, None)``, если диалог не свой."""
    if author not in ("owner", "agent"):
        raise ValueError("author")
    row_id = uuid.uuid4()
    res = await s.execute(
        text(
            "INSERT INTO conversation_messages (id, conversation_id, author, text_enc, "
            "reply_to_id, crisis, client_message_id, reply_lease_until) "
            "SELECT :i, c.id, CAST(:au AS message_author), :t, :rt, :cr, :k, "
            "CASE WHEN CAST(:ls AS double precision) IS NULL THEN NULL "
            "ELSE clock_timestamp() + make_interval(secs => CAST(:ls AS double precision)) END "
            "FROM conversations c "
            "JOIN agents a ON a.id = c.agent_id JOIN users u ON u.id = a.user_id "
            "WHERE c.id = :c AND a.user_id = :u AND u.deleted_at IS NULL "
            "RETURNING id, created_at, reply_lease_until"
        ),
        {
            "i": row_id,
            "c": conversation_id,
            "u": user_id,
            "au": author,
            "t": keys.seal(crypto.CONVERSATION_MESSAGE_TEXT, row_id, body),
            "rt": reply_to,
            "cr": crisis,
            "k": client_message_id,
            "ls": lease_s,
        },
    )
    row = res.one_or_none()
    if row is None:
        return None, None, None
    return row_id, row.created_at, row.reply_lease_until


async def add_owner_turn(
    sm: async_sessionmaker[AsyncSession], keys: Keyring, user_id: uuid.UUID, body: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """Реплика владельца в текущий диалог (создаётся, если его нет) — **одной** транзакцией
    (АР-24: 40P01/40001 → 409, ничего не сохранено, повтор безопасен). → (id диалога, id
    реплики). ``OwnerGone`` — владельца или агента нет (удалён во время запроса)."""
    async with user_scope(sm, user_id) as s:
        conv = await _conversation(s, user_id)
        row_id = await _insert_message(s, keys, user_id, conv, "owner", body)
        if row_id is None:
            raise OwnerGone
    return conv, row_id


@dataclass(frozen=True)
class StoredReply:
    """Сохранённый ответ агента на реплику владельца (повтор с ключом, АР-51)."""

    id: uuid.UUID
    text: str | None  # None — не расшифровался (код — ``unavailable``)
    crisis: bool
    unavailable: str | None = None


TurnState = Literal["new", "unanswered", "answered", "busy", "mismatch"]


@dataclass(frozen=True)
class OwnerTurn:
    """Реплика владельца с ключом идемпотентности ``client_message_id`` (АР-51):

    - ``new`` — сохранена этим запросом; ``unanswered`` — была сохранена раньше, ответа нет,
      никто его не готовит: аренда взята этим запросом. В обоих случаях ``lease`` — до какого
      времени ответ готовит этот запрос (снять — ``release_reply_lease``);
    - ``answered`` — ответ уже сохранён (``reply``); ``busy`` — ответ готовит другой запрос
      (его аренда не истекла); ``mismatch`` — под этим ключом сохранён другой текст.

    ``text`` — сохранённый текст реплики (на него и отвечать; ``None`` — не расшифровался)."""

    state: TurnState
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    created_at: datetime
    text: str | None
    lease: datetime | None = None
    limit_charged: bool = False
    reply: StoredReply | None = None

    @property
    def position(self) -> FeedPosition:
        return FeedPosition(self.created_at, self.message_id)


_TURN_BY_KEY = (
    "SELECT m.id, m.conversation_id, m.text_enc, m.created_at, m.limit_charged, "
    "COALESCE(m.reply_lease_until > clock_timestamp(), false) AS busy, "
    "r.id AS reply_id, r.text_enc AS reply_enc, r.crisis AS reply_crisis "
    "FROM conversation_messages m JOIN conversations c ON c.id = m.conversation_id "
    "JOIN agents a ON a.id = c.agent_id "
    "LEFT JOIN conversation_messages r "
    "ON r.reply_to_id = m.id AND r.conversation_id = m.conversation_id "
    "WHERE a.user_id = :u AND m.author = 'owner' AND m.client_message_id = :k "
    "ORDER BY m.created_at, m.id LIMIT 1"
)


async def accept_owner_turn(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    body: str,
    client_message_id: uuid.UUID,
    *,
    lease_s: float,
) -> OwnerTurn:
    """Реплика владельца с ключом идемпотентности (АР-51) — **одной** транзакцией (АР-24).

    Строка агента владельца блокируется (``_conversation``: ``FOR NO KEY UPDATE``) до поиска
    ключа, поэтому два одновременных запроса с одним ключом идут по очереди: второй видит
    реплику первого (READ COMMITTED) и его аренду — ``busy``. Ключ ищется по **всем** диалогам
    владельца (уникальность в пределах владельца); у другого человека тот же ключ — другая
    реплика. ``OwnerGone`` — владельца или агента нет."""
    async with user_scope(sm, user_id) as s:
        conv = await _conversation(s, user_id)
        row = (
            await s.execute(text(_TURN_BY_KEY), {"u": user_id, "k": client_message_id})
        ).one_or_none()
        if row is None:
            row_id, created, lease = await _insert_row(
                s,
                keys,
                user_id,
                conv,
                "owner",
                body,
                client_message_id=client_message_id,
                lease_s=lease_s,
            )
            if row_id is None or created is None:
                raise OwnerGone
            return OwnerTurn("new", conv, row_id, created, body, lease)
        turn_id, turn_conv = _uuid(row.id), _uuid(row.conversation_id)
        stored, _ = open_row(keys, crypto.CONVERSATION_MESSAGE_TEXT, turn_id, row.text_enc)
        base = OwnerTurn(
            "answered",
            turn_conv,
            turn_id,
            row.created_at,
            stored,
            limit_charged=bool(row.limit_charged),
        )
        if stored is not None and stored != body:
            return replace(base, state="mismatch")
        if row.reply_id is not None:
            reply_id = _uuid(row.reply_id)
            reply, reason = open_row(
                keys, crypto.CONVERSATION_MESSAGE_TEXT, reply_id, row.reply_enc
            )
            return replace(base, reply=StoredReply(reply_id, reply, bool(row.reply_crisis), reason))
        if row.busy:
            return replace(base, state="busy")
        lease = (
            await s.execute(
                text(
                    "UPDATE conversation_messages SET reply_lease_until = clock_timestamp() + "
                    "make_interval(secs => CAST(:ls AS double precision)) "
                    "WHERE id = :i RETURNING reply_lease_until"
                ),
                {"i": turn_id, "ls": lease_s},
            )
        ).scalar_one()
        return replace(base, state="unanswered", lease=lease)


async def release_reply_lease(
    sm: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    message_id: uuid.UUID,
    lease: datetime,
) -> bool:
    """Снять свою аренду ответа на реплику (запрос завершён любым исходом). Аренду, которую
    после истечения взял другой запрос, не трогает (сравнение по значению)."""
    async with user_scope(sm, user_id) as s:
        res = await s.execute(
            text(
                "UPDATE conversation_messages m SET reply_lease_until = NULL "
                "FROM conversations c JOIN agents a ON a.id = c.agent_id "
                "WHERE m.id = :i AND c.id = m.conversation_id AND a.user_id = :u "
                "AND m.reply_lease_until = :l RETURNING m.id"
            ),
            {"i": message_id, "u": user_id, "l": lease},
        )
        return res.one_or_none() is not None


async def mark_limit_charged(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID, message_id: uuid.UUID
) -> bool:
    """Отметить, что дневной лимит за реплику засчитан (АР-32): повтор с тем же ключом его
    повторно не расходует."""
    async with user_scope(sm, user_id) as s:
        res = await s.execute(
            text(
                "UPDATE conversation_messages m SET limit_charged = true "
                "FROM conversations c JOIN agents a ON a.id = c.agent_id "
                "WHERE m.id = :i AND c.id = m.conversation_id AND a.user_id = :u "
                "AND m.author = 'owner' RETURNING m.id"
            ),
            {"i": message_id, "u": user_id},
        )
        return res.one_or_none() is not None


_PAGE_FIRST = (
    "SELECT m.id, m.author::text AS author, m.text_enc, m.created_at, "
    "m.client_message_id, m.reply_to_id, m.crisis "
    "FROM conversation_messages m JOIN conversations c ON c.id = m.conversation_id "
    "JOIN agents a ON a.id = c.agent_id JOIN users u ON u.id = a.user_id "
    "WHERE a.user_id = :u AND u.deleted_at IS NULL "
    "ORDER BY m.created_at DESC, m.id DESC LIMIT :n"
)
_PAGE_BEFORE = (
    "SELECT m.id, m.author::text AS author, m.text_enc, m.created_at, "
    "m.client_message_id, m.reply_to_id, m.crisis "
    "FROM conversation_messages m JOIN conversations c ON c.id = m.conversation_id "
    "JOIN agents a ON a.id = c.agent_id JOIN users u ON u.id = a.user_id "
    "WHERE a.user_id = :u AND u.deleted_at IS NULL AND (m.created_at, m.id) < (:bt, :bi) "
    "ORDER BY m.created_at DESC, m.id DESC LIMIT :n"
)


@dataclass(frozen=True)
class ChatRow(ConversationText):
    """Реплика ленты личного чата: ключ клиента у реплики владельца, у ответа агента — реплика,
    на которую он отвечает, и флаг кризисного протокола (АР-51)."""

    client_message_id: uuid.UUID | None = None
    reply_to_id: uuid.UUID | None = None
    crisis: bool = False


@dataclass(frozen=True)
class FeedPosition:
    """Позиция реплики в ленте личного чата (ключ курсора ``GET /chat/messages``)."""

    created_at: datetime
    id: uuid.UUID


async def read_chat_page(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    *,
    limit: int,
    before: FeedPosition | None = None,
) -> tuple[list[ChatRow], bool]:
    """Страница ленты личного чата владельца — реплики **всех** его диалогов от новых к старым
    (``created_at DESC, id DESC``), строго старше ``before``. → (реплики, есть ли старше).
    Нерасшифрованная строка не пропускается: ``text = None``, код — в ``unavailable``. Явный
    отбор по владельцу (``agents.user_id``) поверх RLS; удалённый владелец — пустая страница."""
    if limit < 1:
        raise ValueError("limit")
    params: dict[str, object] = {"u": user_id, "n": limit + 1}
    sql = _PAGE_FIRST
    if before is not None:
        sql = _PAGE_BEFORE
        params |= {"bt": before.created_at, "bi": before.id}
    async with user_scope(sm, user_id) as s:
        rows = (await s.execute(text(sql), params)).all()
    items: list[ChatRow] = []
    for r in rows[:limit]:
        row_id = _uuid(r.id)
        body, reason = open_row(keys, crypto.CONVERSATION_MESSAGE_TEXT, row_id, r.text_enc)
        items.append(
            ChatRow(
                row_id,
                r.author,
                body,
                r.created_at,
                reason,
                client_message_id=None
                if r.client_message_id is None
                else _uuid(r.client_message_id),
                reply_to_id=None if r.reply_to_id is None else _uuid(r.reply_to_id),
                crisis=bool(r.crisis),
            )
        )
    return items, len(rows) > limit


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


async def read_history_head(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    limit: int,
) -> tuple[HistorySummary | None, int, list[ConversationText]]:
    """Сводка, число реплик после её отметки и первые ``limit`` из них (для раунда сжатия: весь
    хвост не читается и не расшифровывается). ``LookupError`` — диалог не свой."""
    async with user_scope(sm, user_id) as s:
        own, summary, _ = await _summary(s, keys, user_id, conversation_id)
        if not own:
            raise LookupError("диалог не найден среди своих")
        total, head = await _turns_head(s, keys, user_id, conversation_id, summary, limit)
    return summary, total, head


async def save_history_summary(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    summary: HistorySummary,
    *,
    expected_through_id: uuid.UUID | None,
    expected: HistorySummary | None = None,
) -> bool:
    """Записать сводку, если в БД всё ещё та, что прочитана: отметка ``expected_through_id``, а
    при переданном ``expected`` — весь конверт (текст, отметка, пометка «на очистку»; ревью 2.4
    р2 MAJOR-2). Сводка, помеченная удалением записи памяти после чтения (АР-37), не
    затирается: иначе пометка пропала бы, а удалённое из прежнего текста вернулось. Без
    ``expected`` помеченная сводка тоже не перезаписывается. False — ничего не меняется (и при
    чужом диалоге или удалённом владельце, Б-44). Шифрование — ``conversations|summary_enc|<id>``.
    """
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
        current: HistorySummary | None = None
        if row.summary_enc is not None:
            raw, _ = open_row(keys, crypto.CONVERSATION_SUMMARY, conversation_id, row.summary_enc)
            current = unpack_summary(raw) if raw is not None else None
        if (current.through_id if current else None) != expected_through_id:
            return False
        if expected is not None:
            if current is None or (current.text, current.forget) != (
                expected.text,
                expected.forget,
            ):
                return False
        elif current is not None and current.forget:
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


async def set_memory_embedding(
    sm: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    item_id: uuid.UUID,
    vector: Sequence[float],
    model: str,
) -> bool:
    """Вектор и модель записи своей памяти (для 2.4) — функцией ``app_set_memory_embedding``
    (АР-35). Чужая, удалённая, без текста или несуществующая — False. Удаление записи (2.4)
    обнуляет ``text_enc``, ``embedding`` и ``embedding_model`` одним UPDATE (CHECK 0001, 0008)."""
    async with user_scope(sm, user_id) as s:
        return bool(
            (
                await s.execute(
                    text("SELECT app_set_memory_embedding(:i, :v, :m)"),
                    {"i": item_id, "v": to_pgvector(vector), "m": model},
                )
            ).scalar_one()
        )


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

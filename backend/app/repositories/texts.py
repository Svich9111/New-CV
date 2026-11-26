"""Слой доступа к зашифрованным текстам (ТЗ §9.1 п.3, задача 4.2; АР-8, АР-11).

Тексты диалога, памяти, черновиков и сообщений пары пишутся в колонки `*_enc` только
шифротекстом (`app.core.crypto`) и расшифровываются здесь же при чтении. Все запросы — внутри
`user_scope` (RLS: только данные владельца и его пары). id строки создаётся приложением до
записи — он входит в связанные данные шифротекста.

Функции БД (`app_send_draft`, `app_couple_feed`, `app_read_original`, `pipeline_*`) ключа не
знают и переносят шифротекст как непрозрачные байты. Оригинал сообщения пары — копия
шифротекста черновика, поэтому он расшифровывается с контекстом черновика
(`crypto.MESSAGE_ORIGINAL`, id = `partner_messages.draft_id`).

Это помощники для задач 2.x/3.x (чат с агентом, память, черновики, лента): эндпоинтов,
которые пишут тексты, до 4.2 не было. Тексты в журналы не пишутся; параметры запросов скрыты
(`make_engine(hide_parameters=True)`)."""

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import crypto
from app.core.crypto import Keyring
from app.core.fingerprint import FingerprintKeys
from app.db import user_scope

Author = Literal["owner", "agent"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConversationText:
    id: uuid.UUID
    author: str
    text: str | None
    created_at: datetime
    # Код причины (DecryptionError.reason), если текст не расшифровался; тогда text = None.
    unavailable: str | None = None


@dataclass(frozen=True)
class MemoryText:
    id: uuid.UUID
    type: str
    source: str
    text: str | None
    unavailable: str | None = None


@dataclass(frozen=True)
class DraftText:
    id: uuid.UUID
    text: str
    version: int
    status: str
    reply_to_id: uuid.UUID | None = None


@dataclass(frozen=True)
class FeedText:
    id: uuid.UUID
    outgoing: bool
    text: str | None
    is_adapted: bool
    status: str
    sent_at: datetime
    unavailable: str | None = None


def _uuid(value: object) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def open_row(
    keys: Keyring, field: crypto.Field, row_id: uuid.UUID | None, blob: bytes
) -> tuple[str | None, str | None]:
    """Чтение одной строки из многих: отказ расшифровки не роняет весь список.
    `row_id=None` — контекст потерян (код `context`). → (текст, None) или (None, код причины).
    В журнал — только код причины и колонка: ни текста, ни id пользователя или строки
    (ТЗ §9.1 п.5)."""
    try:
        if row_id is None:
            raise crypto.DecryptionError(crypto.CONTEXT_MISSING)
        return keys.open(field, row_id, blob), None
    except crypto.DecryptionError as exc:
        logger.warning(
            "encrypted text unavailable: field=%s.%s reason=%s",
            field.table,
            field.column,
            exc.reason,
        )
        return None, exc.reason


# --- Личный чат с агентом (ФТ-05) ---------------------------------------------------------


async def add_conversation_message(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    author: Author,
    body: str,
) -> uuid.UUID:
    """Реплика в свой диалог; чужой диалог отклонит RLS (WITH CHECK)."""
    row_id = uuid.uuid4()
    async with user_scope(sm, user_id) as s:
        await s.execute(
            text(
                "INSERT INTO conversation_messages (id, conversation_id, author, text_enc) "
                "VALUES (:i, :c, CAST(:a AS message_author), :t)"
            ),
            {
                "i": row_id,
                "c": conversation_id,
                "a": author,
                "t": keys.seal(crypto.CONVERSATION_MESSAGE_TEXT, row_id, body),
            },
        )
    return row_id


async def list_conversation_messages(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> list[ConversationText]:
    async with user_scope(sm, user_id) as s:
        rows = await s.execute(
            text(
                "SELECT id, author::text AS author, text_enc, created_at "
                "FROM conversation_messages WHERE conversation_id = :c ORDER BY created_at, id"
            ),
            {"c": conversation_id},
        )
        result = []
        for r in rows:
            row_id = _uuid(r.id)
            body, reason = open_row(keys, crypto.CONVERSATION_MESSAGE_TEXT, row_id, r.text_enc)
            result.append(ConversationText(row_id, r.author, body, r.created_at, reason))
        return result


async def set_conversation_summary(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    summary: str,
) -> bool:
    """False — диалог не найден среди своих."""
    async with user_scope(sm, user_id) as s:
        res = await s.execute(
            text("UPDATE conversations SET summary_enc = :t WHERE id = :c RETURNING id"),
            {
                "c": conversation_id,
                "t": keys.seal(crypto.CONVERSATION_SUMMARY, conversation_id, summary),
            },
        )
        return res.one_or_none() is not None


async def get_conversation_summary(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> str | None:
    async with user_scope(sm, user_id) as s:
        blob = (
            await s.execute(
                text("SELECT summary_enc FROM conversations WHERE id = :c"), {"c": conversation_id}
            )
        ).scalar_one_or_none()
    return keys.open_optional(crypto.CONVERSATION_SUMMARY, conversation_id, blob)


# --- Профиль агента (ФТ-02) ----------------------------------------------------------------


async def set_agent_profile_summary(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    agent_id: uuid.UUID,
    summary: str,
) -> bool:
    async with user_scope(sm, user_id) as s:
        res = await s.execute(
            text("UPDATE agents SET profile_summary_enc = :t WHERE id = :a RETURNING id"),
            {"a": agent_id, "t": keys.seal(crypto.AGENT_PROFILE_SUMMARY, agent_id, summary)},
        )
        return res.one_or_none() is not None


async def get_agent_profile_summary(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> str | None:
    async with user_scope(sm, user_id) as s:
        blob = (
            await s.execute(
                text("SELECT profile_summary_enc FROM agents WHERE id = :a"), {"a": agent_id}
            )
        ).scalar_one_or_none()
    return keys.open_optional(crypto.AGENT_PROFILE_SUMMARY, agent_id, blob)


# --- Память агента (ФТ-12…ФТ-15) -------------------------------------------------------------


async def add_memory_item(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    agent_id: uuid.UUID,
    memory_type: str,
    source: str,
    body: str,
    *,
    fingerprints: FingerprintKeys,
    event_date: date | None = None,
    confidence: float = 1.0,
    source_message_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Запись памяти своего агента: текст — шифротекстом, отпечаток (АР-36) — обязателен, чтобы
    удаление всегда оставляло отпечаток (Б-68: CHECK ``text_enc IS NULL OR fingerprint IS NOT
    NULL``). Вектор — отдельно, функцией ``app_set_memory_embedding`` (Б-63)."""
    row_id = uuid.uuid4()
    async with user_scope(sm, user_id) as s:
        await s.execute(
            text(
                "INSERT INTO memory_items (id, agent_id, type, source, text_enc, fingerprint, "
                "event_date, confidence, source_message_id) VALUES "
                "(:i, :a, CAST(:ty AS memory_type), CAST(:so AS memory_source), :t, :fp, :d, "
                ":c, :sm)"
            ),
            {
                "i": row_id,
                "a": agent_id,
                "ty": memory_type,
                "so": source,
                "t": keys.seal(crypto.MEMORY_TEXT, row_id, body),
                "fp": fingerprints.of(agent_id, body),
                "d": event_date,
                "c": confidence,
                "sm": source_message_id,
            },
        )
    return row_id


async def list_memory_items(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> list[MemoryText]:
    """Неудалённые записи памяти своего агента."""
    async with user_scope(sm, user_id) as s:
        rows = await s.execute(
            text(
                "SELECT id, type::text AS type, source::text AS source, text_enc "
                "FROM memory_items WHERE agent_id = :a AND deleted_at IS NULL "
                "AND text_enc IS NOT NULL ORDER BY created_at, id"
            ),
            {"a": agent_id},
        )
        result = []
        for r in rows:
            row_id = _uuid(r.id)
            body, reason = open_row(keys, crypto.MEMORY_TEXT, row_id, r.text_enc)
            result.append(MemoryText(row_id, r.type, r.source, body, reason))
        return result


# --- Черновик и сообщение пары (ФТ-07…ФТ-11) -------------------------------------------------


async def create_draft(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    agent_id: uuid.UUID,
    body: str,
    reply_to_id: uuid.UUID | None = None,
) -> uuid.UUID:
    row_id = uuid.uuid4()
    async with user_scope(sm, user_id) as s:
        await s.execute(
            text(
                "INSERT INTO message_drafts (id, agent_id, text_enc, reply_to_id) "
                "VALUES (:i, :a, :t, :r)"
            ),
            {
                "i": row_id,
                "a": agent_id,
                "t": keys.seal(crypto.DRAFT_TEXT, row_id, body),
                "r": reply_to_id,
            },
        )
    return row_id


async def update_draft_text(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    draft_id: uuid.UUID,
    body: str,
) -> int | None:
    """Правка текста черновика → новая версия; None — черновик не найден или уже не меняется."""
    async with user_scope(sm, user_id) as s:
        res = await s.execute(
            text(
                "UPDATE message_drafts SET text_enc = :t, version = version + 1, "
                "updated_at = now() WHERE id = :d AND status IN ('draft', 'approved') "
                "RETURNING version"
            ),
            {"d": draft_id, "t": keys.seal(crypto.DRAFT_TEXT, draft_id, body)},
        )
        version = res.scalar_one_or_none()
    return None if version is None else int(version)


async def own_agent_id(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> uuid.UUID | None:
    """Агент владельца (живого); None — нет агента или владелец удалён."""
    async with user_scope(sm, user_id) as s:
        value = (
            await s.execute(
                text(
                    "SELECT a.id FROM agents a JOIN users u ON u.id = a.user_id "
                    "WHERE a.user_id = :u AND u.deleted_at IS NULL"
                ),
                {"u": user_id},
            )
        ).scalar_one_or_none()
    return None if value is None else _uuid(value)


async def reply_target_ok(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID, message_id: uuid.UUID
) -> bool:
    """«Ответить с агентом» (ТЗ §4.4 п.5): сообщение — **полученное** владельцем, доставленное
    (``delivered``/``read``/``answered``) в его **текущей активной** паре — как проверит
    ``app_send_draft`` (FA004) при отправке."""
    async with user_scope(sm, user_id) as s:
        value = (
            await s.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM partner_messages pm "
                    "JOIN couple_members cm ON cm.couple_id = pm.couple_id AND cm.user_id = :u "
                    "AND cm.active JOIN couples c ON c.id = pm.couple_id AND c.status = 'active' "
                    "WHERE pm.id = :m AND pm.recipient_id = :u "
                    "AND pm.status IN ('delivered', 'read', 'answered'))"
                ),
                {"u": user_id, "m": message_id},
            )
        ).scalar_one()
    return bool(value)


async def replace_draft_text(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    draft_id: uuid.UUID,
    body: str,
    *,
    expected_version: int,
) -> int | None:
    """«Другой вариант» (ФТ-07): новая версия черновика только в статусе ``draft`` и только если
    версия всё ещё ``expected_version`` (автор не правил его, пока шла модель). None — иначе."""
    async with user_scope(sm, user_id) as s:
        res = await s.execute(
            text(
                "UPDATE message_drafts SET text_enc = :t, version = version + 1, "
                "updated_at = now() WHERE id = :d AND status = 'draft' AND version = :v "
                "RETURNING version"
            ),
            {
                "d": draft_id,
                "v": expected_version,
                "t": keys.seal(crypto.DRAFT_TEXT, draft_id, body),
            },
        )
        version = res.scalar_one_or_none()
    return None if version is None else int(version)


async def get_draft(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    draft_id: uuid.UUID,
) -> DraftText | None:
    async with user_scope(sm, user_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT id, text_enc, version, status::text AS status, reply_to_id "
                    "FROM message_drafts WHERE id = :d"
                ),
                {"d": draft_id},
            )
        ).one_or_none()
    if row is None:
        return None
    return DraftText(
        id=draft_id,
        text=keys.open(crypto.DRAFT_TEXT, draft_id, row.text_enc),
        version=row.version,
        status=row.status,
        reply_to_id=None if row.reply_to_id is None else _uuid(row.reply_to_id),
    )


async def send_draft(
    sm: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    draft_id: uuid.UUID,
    expected_version: int,
) -> uuid.UUID:
    """«Отправить» (ФТ-08): `app_send_draft` переносит шифротекст черновика в оригинал
    сообщения без расшифровки. Ошибки — как DBAPIError (обработка — задача 3.x): FA001…FA004
    (DECISIONS АР-13) и с миграции 0005 — 42501: нет входа, пользователь не найден или помечен
    удалённым (раньше в этих случаях было FA001)."""
    async with user_scope(sm, user_id) as s:
        value = (
            await s.execute(
                text("SELECT app_send_draft(:d, :v)"), {"d": draft_id, "v": expected_version}
            )
        ).scalar_one()
    return _uuid(value)


async def read_feed(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    limit: int = 50,
    before: datetime | None = None,
) -> list[FeedText]:
    """Лента пары (ФТ-11): автору — его оригинал, получателю — версия для получателя."""
    async with user_scope(sm, user_id) as s:
        rows = await s.execute(
            text(
                "SELECT f.id, f.outgoing, f.text_enc, f.is_adapted, f.status::text AS status, "
                "f.sent_at, pm.draft_id FROM app_couple_feed(:l, :b) f "
                "LEFT JOIN partner_messages pm ON pm.id = f.id ORDER BY f.sent_at DESC"
            ),
            {"l": limit, "b": before},
        )
        result = []
        for r in rows:
            msg_id = _uuid(r.id)
            body: str | None = None
            reason: str | None = None
            if r.text_enc is None:
                pass
            elif not r.outgoing:
                body, reason = open_row(keys, crypto.MESSAGE_ADAPTED, msg_id, r.text_enc)
            else:
                # Оригинал — с контекстом черновика; черновик удалён → `context` (Б-38).
                draft = None if r.draft_id is None else _uuid(r.draft_id)
                body, reason = open_row(keys, crypto.MESSAGE_ORIGINAL, draft, r.text_enc)
            result.append(
                FeedText(msg_id, r.outgoing, body, r.is_adapted, r.status, r.sent_at, reason)
            )
        return result


def _open_original(keys: Keyring, draft_id: object, blob: bytes) -> str:
    """Оригинал расшифровывается с контекстом своего черновика; без черновика — отказ с
    кодом `context` (потерян контекст), отличимым от подделки (`auth`)."""
    if draft_id is None:
        raise crypto.DecryptionError(crypto.CONTEXT_MISSING)
    return keys.open(crypto.MESSAGE_ORIGINAL, _uuid(draft_id), blob)


async def read_original(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    message_id: uuid.UUID,
) -> str | None:
    """«Показать оригинал» (ФТ-10): получателю — с фиксацией просмотра (функция БД).
    Расшифровка — внутри той же транзакции: если она не удалась, `DecryptionError` откатывает
    и отметку `original_viewed_at` — просмотр засчитывается, только когда текст показан (ТЗ §7)."""
    async with user_scope(sm, user_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT app_read_original(:m) AS blob, "
                    "(SELECT draft_id FROM partner_messages WHERE id = :m) AS draft_id"
                ),
                {"m": message_id},
            )
        ).one()
        if row.blob is None:
            return None
        return _open_original(keys, row.draft_id, row.blob)


# --- Конвейер (задача 3.2): сессия уже под ролью feelings_worker --------------------------


async def pipeline_read(
    s: AsyncSession, keys: Keyring, message_id: uuid.UUID, token: uuid.UUID
) -> str | None:
    """Одобренный оригинал для воркера конвейера по своей аренде (0013, Б-39, Б-48):
    ``pipeline_read`` отдаёт шифротекст и id черновика (контекст расшифровки) только если
    сообщение в ``processing``, токен аренды свой и черновик отправлен автором («Отправить»).
    ``None`` — нет (аренду перехватили, сообщение удалено с аккаунтом автора, черновик не
    ``sent``): одобрение не выдаётся. Не расшифровался — ``DecryptionError`` (не мусор)."""
    row = (
        await s.execute(
            text("SELECT original_enc, draft_id FROM pipeline_read(:m, :t)"),
            {"m": message_id, "t": token},
        )
    ).one_or_none()
    return None if row is None else _open_original(keys, row.draft_id, row.original_enc)

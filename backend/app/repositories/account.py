"""Слой доступа: удаление аккаунта и выгрузка своих данных (ФТ-21; задача 4.5, этап 2).

Удаление — только функцией БД `app_delete_account()` (АР-23): у приложения нет прямого DELETE
на `users` (Б-19). Выгрузка — одна транзакция `user_scope` текущего пользователя: каждый
раздел читается под RLS и дополнительно отбирается по владельцу (`agents.user_id`,
`couple_members.user_id`, `subscriptions.payer_id`), сообщения пары — функцией
`app_export_partner_messages()` (миграция 0006; у `feelings_app` нет SELECT на `original_enc`,
АР-11).

Тексты расшифровываются построчно (`texts.open_row`): повреждённая строка даёт `UnavailableText`
с кодом причины, выгрузка не падает; в журнал — только колонка и код причины, без текста и id
(ТЗ §9.1 п.5). Отказ расшифровки не откатывает транзакцию, поэтому отметка просмотра оригинала,
которую ставит функция выгрузки (АР-25 п.2), сохраняется и для нерасшифрованного оригинала:
выгрузка — это и есть выдача по запросу (решение 4.5 этап 2, A2)."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import crypto
from app.core.crypto import Keyring
from app.db import sqlstate, user_scope
from app.repositories import users
from app.repositories.chat import summary_text
from app.repositories.texts import open_row
from app.repositories.users import ONBOARDED_SQL
from app.schemas import (
    AccountExport,
    ExportAgent,
    ExportConversation,
    ExportConversationMessage,
    ExportCouple,
    ExportCoupleMessages,
    ExportDraft,
    ExportMemoryItem,
    ExportPayment,
    ExportProfile,
    ExportReceivedMessage,
    ExportSafetyEvent,
    ExportSentMessage,
    ExportSubscription,
    ExportText,
    UnavailableText,
)


class AccountGone(Exception):
    """Пользователя нет или он удалён (42501 функций БД, строки профиля нет): для API — 401."""


@asynccontextmanager
async def _account_errors(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> AsyncIterator[None]:
    """42501 → AccountGone, только если пользователя действительно нет: код 42501 у функций
    аккаунта значит и «нет пользователя» (их собственный RAISE), и «нет права» (отозван EXECUTE,
    сбой RLS) — по коду их не различить, а текст и HINT ошибки не контракт. Поэтому при 42501
    существование неудалённого пользователя перепроверяется в новой транзакции (прежняя уже
    отменена): нет — 401; есть — сбой прав, ошибка пробрасывается (500), живого пользователя
    не разлогинивают (ревью 4.5 этап 2 MINOR-1, тест Э2-M1). Остальные ошибки БД (в т.ч.
    40P01/40001 — их переводит в 409 общий обработчик, АР-24) — как есть."""
    try:
        yield
    except DBAPIError as exc:
        if sqlstate(exc) == "42501" and not await users.is_active(sm, user_id):
            raise AccountGone from exc
        raise


def _uuid(value: object) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _text(keys: Keyring, field: crypto.Field, row_id: object, blob: bytes) -> ExportText:
    """`row_id=None` — контекст расшифровки потерян (код `context`)."""
    body, reason = open_row(keys, field, None if row_id is None else _uuid(row_id), blob)
    if body is not None:
        return body
    return UnavailableText(reason=cast(Any, reason))


def _optional_text(
    keys: Keyring, field: crypto.Field, row_id: object, blob: bytes | None
) -> ExportText | None:
    return None if blob is None else _text(keys, field, row_id, blob)


def _summary(keys: Keyring, conversation_id: object, blob: bytes | None) -> ExportText | None:
    """Сводка диалога для людей: без служебной отметки конверта (задача 2.1, `chat`)."""
    value = _optional_text(keys, crypto.CONVERSATION_SUMMARY, conversation_id, blob)
    return summary_text(value) if isinstance(value, str) else value


# --- Удаление ---------------------------------------------------------------------------------


async def delete_account(sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> None:
    """Полное удаление (АР-23): пара распускается как при выходе, данные удаляются каскадом,
    аудит пишет сама функция (АР-24). Нет пользователя или уже удалён — AccountGone."""
    async with _account_errors(sm, user_id):
        async with user_scope(sm, user_id) as s:
            await s.execute(text("SELECT app_delete_account()"))


# --- Выгрузка ---------------------------------------------------------------------------------


async def _couple_messages(
    s: AsyncSession, keys: Keyring
) -> tuple[list[ExportSentMessage], list[ExportReceivedMessage]]:
    """Функция 0006: свои отправленные во всех статусах, полученные — только доставленные;
    у полученных выдача оригинала фиксируется как просмотр. `draft_id` — только для AAD."""
    rows = await s.execute(
        text(
            "SELECT id, couple_id, outgoing, draft_id, reply_to_id, shown_enc, shown_is_original, "
            "original_enc, is_adapted, harsh_tone_warning, status::text AS status, sent_at, "
            "delivered_at, read_at, answered_at, original_viewed_at "
            "FROM app_export_partner_messages()"
        )
    )
    sent: list[ExportSentMessage] = []
    received: list[ExportReceivedMessage] = []
    for r in rows:
        original = _text(keys, crypto.MESSAGE_ORIGINAL, r.draft_id, r.original_enc)
        if r.outgoing:
            sent.append(
                ExportSentMessage(
                    id=r.id,
                    couple_id=r.couple_id,
                    reply_to_id=r.reply_to_id,
                    original=original,
                    is_adapted=r.is_adapted,
                    harsh_tone_warning=r.harsh_tone_warning,
                    status=r.status,
                    sent_at=r.sent_at,
                    delivered_at=r.delivered_at,
                    read_at=r.read_at,
                    answered_at=r.answered_at,
                )
            )
            continue
        if r.shown_is_original:
            # Версии для получателя нет — показан оригинал (как в ленте): контекст черновика
            # автора.
            shown = _text(keys, crypto.MESSAGE_ORIGINAL, r.draft_id, r.shown_enc)
        else:
            shown = _text(keys, crypto.MESSAGE_ADAPTED, r.id, r.shown_enc)
        received.append(
            ExportReceivedMessage(
                id=r.id,
                couple_id=r.couple_id,
                reply_to_id=r.reply_to_id,
                text=shown,
                is_adapted=r.is_adapted,
                harsh_tone_warning=r.harsh_tone_warning,
                original=original,
                original_viewed_at=r.original_viewed_at,
                status=r.status,
                sent_at=r.sent_at,
                read_at=r.read_at,
            )
        )
    return sent, received


async def _profile(s: AsyncSession, user_id: uuid.UUID) -> tuple[ExportProfile, datetime]:
    row = (
        await s.execute(
            text(
                "SELECT id, telegram_id, name, address_form, partner_name, timezone, created_at, "  # noqa: S608 — константа ONBOARDED_SQL, ввода нет
                "consent_terms_at, consent_data_at, consent_sensitive_at, age_confirmed_at, "
                f"{ONBOARDED_SQL} AS onboarded, now() AS exported_at "
                "FROM users WHERE id = :id AND deleted_at IS NULL"
            ),
            {"id": user_id},
        )
    ).one_or_none()
    if row is None:
        raise AccountGone
    profile = ExportProfile(
        id=row.id,
        telegram_id=row.telegram_id,
        name=row.name,
        address_form=row.address_form,
        partner_name=row.partner_name,
        timezone=row.timezone,
        created_at=row.created_at,
        consent_terms_at=row.consent_terms_at,
        consent_data_at=row.consent_data_at,
        consent_sensitive_at=row.consent_sensitive_at,
        age_confirmed_at=row.age_confirmed_at,
        onboarding_completed=row.onboarded,
    )
    return profile, row.exported_at


async def _agent(
    s: AsyncSession, keys: Keyring, user_id: uuid.UUID
) -> tuple[ExportAgent | None, uuid.UUID | None]:
    row = (
        await s.execute(
            text(
                "SELECT id, created_at, profile_summary_enc, tone_settings FROM agents "
                "WHERE user_id = :u"
            ),
            {"u": user_id},
        )
    ).one_or_none()
    if row is None:
        return None, None
    agent = ExportAgent(
        created_at=row.created_at,
        profile_summary=_optional_text(
            keys, crypto.AGENT_PROFILE_SUMMARY, row.id, row.profile_summary_enc
        ),
        tone_settings=dict(row.tone_settings or {}),
    )
    return agent, _uuid(row.id)


async def _conversations(
    s: AsyncSession, keys: Keyring, agent_id: uuid.UUID
) -> list[ExportConversation]:
    convs = (
        await s.execute(
            text(
                "SELECT id, started_at, summary_enc FROM conversations WHERE agent_id = :a "
                "ORDER BY started_at, id"
            ),
            {"a": agent_id},
        )
    ).all()
    msgs = await s.execute(
        text(
            "SELECT m.id, m.conversation_id, m.author::text AS author, m.text_enc, m.created_at "
            "FROM conversation_messages m JOIN conversations c ON c.id = m.conversation_id "
            "WHERE c.agent_id = :a ORDER BY m.created_at, m.id"
        ),
        {"a": agent_id},
    )
    by_conv: dict[uuid.UUID, list[ExportConversationMessage]] = {}
    for m in msgs:
        by_conv.setdefault(_uuid(m.conversation_id), []).append(
            ExportConversationMessage(
                id=m.id,
                author=m.author,
                text=_text(keys, crypto.CONVERSATION_MESSAGE_TEXT, m.id, m.text_enc),
                created_at=m.created_at,
            )
        )
    return [
        ExportConversation(
            id=c.id,
            started_at=c.started_at,
            summary=_summary(keys, c.id, c.summary_enc),
            messages=by_conv.get(_uuid(c.id), []),
        )
        for c in convs
    ]


async def _memory(s: AsyncSession, keys: Keyring, agent_id: uuid.UUID) -> list[ExportMemoryItem]:
    """Действующие записи; удалённые (ФТ-13) текста не имеют и не выгружаются, отпечатки и
    векторы — служебные данные (АР-25 п.6)."""
    rows = await s.execute(
        text(
            "SELECT id, type::text AS type, text_enc, event_date, source::text AS source, "
            "source_message_id, confidence, created_at, updated_at FROM memory_items "
            "WHERE agent_id = :a AND deleted_at IS NULL AND text_enc IS NOT NULL "
            "ORDER BY created_at, id"
        ),
        {"a": agent_id},
    )
    return [
        ExportMemoryItem(
            id=r.id,
            type=r.type,
            text=_text(keys, crypto.MEMORY_TEXT, r.id, r.text_enc),
            event_date=r.event_date,
            source=r.source,
            source_message_id=r.source_message_id,
            confidence=r.confidence,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]


async def _drafts(s: AsyncSession, keys: Keyring, agent_id: uuid.UUID) -> list[ExportDraft]:
    rows = await s.execute(
        text(
            "SELECT id, text_enc, reply_to_id, version, status::text AS status, created_at, "
            "updated_at FROM message_drafts WHERE agent_id = :a ORDER BY created_at, id"
        ),
        {"a": agent_id},
    )
    return [
        ExportDraft(
            id=r.id,
            text=_text(keys, crypto.DRAFT_TEXT, r.id, r.text_enc),
            reply_to_id=r.reply_to_id,
            version=r.version,
            status=r.status,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]


async def _couples(s: AsyncSession, user_id: uuid.UUID) -> list[ExportCouple]:
    """Своё участие, включая распавшиеся пары; о партнёре — ничего (АР-25 п.4)."""
    rows = await s.execute(
        text(
            "SELECT c.id, c.status::text AS status, cm.role::text AS role, c.created_at, "
            "c.activated_at, c.dissolved_at, cm.joined_at, cm.left_at "
            "FROM couple_members cm JOIN couples c ON c.id = cm.couple_id "
            "WHERE cm.user_id = :u ORDER BY cm.joined_at, c.id"
        ),
        {"u": user_id},
    )
    return [
        ExportCouple(
            id=r.id,
            status=r.status,
            role=r.role,
            created_at=r.created_at,
            activated_at=r.activated_at,
            dissolved_at=r.dissolved_at,
            joined_at=r.joined_at,
            left_at=r.left_at,
        )
        for r in rows
    ]


async def _subscriptions(s: AsyncSession, user_id: uuid.UUID) -> list[ExportSubscription]:
    """Подписки, где пользователь — плательщик, с их платежами (АР-25 п.6)."""
    subs = (
        await s.execute(
            text(
                "SELECT id, couple_id, plan, status::text AS status, period_start, period_end, "
                "created_at FROM subscriptions WHERE payer_id = :u ORDER BY created_at, id"
            ),
            {"u": user_id},
        )
    ).all()
    pays = await s.execute(
        text(
            "SELECT p.id, p.subscription_id, p.amount, p.currency, p.provider, "
            "p.provider_charge_id, p.status::text AS status, p.created_at "
            "FROM payments p JOIN subscriptions s ON s.id = p.subscription_id "
            "WHERE s.payer_id = :u ORDER BY p.created_at, p.id"
        ),
        {"u": user_id},
    )
    by_sub: dict[uuid.UUID, list[ExportPayment]] = {}
    for p in pays:
        by_sub.setdefault(_uuid(p.subscription_id), []).append(
            ExportPayment(
                id=p.id,
                amount=p.amount,
                currency=p.currency,
                provider=p.provider,
                provider_charge_id=p.provider_charge_id,
                status=p.status,
                created_at=p.created_at,
            )
        )
    return [
        ExportSubscription(
            id=r.id,
            couple_id=r.couple_id,
            plan=r.plan,
            status=r.status,
            period_start=r.period_start,
            period_end=r.period_end,
            created_at=r.created_at,
            payments=by_sub.get(_uuid(r.id), []),
        )
        for r in subs
    ]


async def _safety_events(s: AsyncSession) -> list[ExportSafetyEvent]:
    """`safety_events` — только тип и время (ТЗ §7, §9.3). У `feelings_app` на таблице только
    INSERT; свои события отдаёт функция `app_export_safety_events()` (миграция 0007) — без
    id события и пользователя."""
    rows = await s.execute(
        text("SELECT type::text AS type, created_at FROM app_export_safety_events()")
    )
    return [ExportSafetyEvent(type=r.type, created_at=r.created_at) for r in rows]


async def export_account(
    sm: async_sessionmaker[AsyncSession], keys: Keyring, user_id: uuid.UUID
) -> AccountExport:
    """Все свои данные одним документом (ФТ-21, АР-25), в одной транзакции. Первой —
    функция сообщений пары: она берёт блокировки в порядке АР-13 (пользователь → пары →
    сообщения) и отвечает 42501, если пользователя уже нет."""
    async with _account_errors(sm, user_id):
        async with user_scope(sm, user_id) as s:
            sent, received = await _couple_messages(s, keys)
            profile, exported_at = await _profile(s, user_id)
            agent, agent_id = await _agent(s, keys, user_id)
            if agent_id is None:
                conversations: list[ExportConversation] = []
                memory: list[ExportMemoryItem] = []
                drafts: list[ExportDraft] = []
            else:
                conversations = await _conversations(s, keys, agent_id)
                memory = await _memory(s, keys, agent_id)
                drafts = await _drafts(s, keys, agent_id)
            return AccountExport(
                exported_at=exported_at,
                profile=profile,
                agent=agent,
                conversations=conversations,
                memory=memory,
                drafts=drafts,
                couple_messages=ExportCoupleMessages(sent=sent, received=received),
                couples=await _couples(s, user_id),
                subscriptions=await _subscriptions(s, user_id),
                safety_events=await _safety_events(s),
            )

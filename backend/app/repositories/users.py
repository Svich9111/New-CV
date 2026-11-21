"""Слой доступа к пользователям. Все запросы — внутри user_scope (RLS)."""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import user_scope
from app.schemas import Me, OnboardingRequest


async def upsert_by_telegram_id(
    sm: async_sessionmaker[AsyncSession], telegram_id: int
) -> uuid.UUID | None:
    """Найти или создать пользователя. None — аккаунт удалён (повторная регистрация — 4.5)."""
    async with user_scope(sm, None) as s:
        row = await s.execute(text("SELECT app_upsert_user(:tg)"), {"tg": telegram_id})
        value = row.scalar_one()
        return uuid.UUID(str(value)) if value is not None else None


async def is_active(sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> bool:
    async with user_scope(sm, user_id) as s:
        row = await s.execute(
            text("SELECT EXISTS (SELECT 1 FROM users WHERE id = :id AND deleted_at IS NULL)"),
            {"id": user_id},
        )
        return bool(row.scalar_one())


async def is_onboarded(sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> bool:
    """Все три согласия и 18+ отмечены (ФТ-02); удалённый пользователь — False."""
    async with user_scope(sm, user_id) as s:
        row = await s.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM users WHERE id = :id AND deleted_at IS NULL "
                "  AND consent_terms_at IS NOT NULL AND consent_data_at IS NOT NULL "
                "  AND consent_sensitive_at IS NOT NULL AND age_confirmed_at IS NOT NULL)"
            ),
            {"id": user_id},
        )
        return bool(row.scalar_one())


async def complete_onboarding(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID, data: OnboardingRequest
) -> bool:
    """Сохранить профиль и согласия, создать личного агента (одного на пользователя).

    Повтор обновляет профиль; время первого согласия не переписывается. False — пользователя
    нет или он удалён. Профиль и агент — в одной транзакции."""
    async with user_scope(sm, user_id) as s:
        updated = await s.execute(
            text(
                "UPDATE users SET name = :name, address_form = :address_form, "
                "  partner_name = :partner_name, timezone = :timezone, "
                "  consent_terms_at = COALESCE(consent_terms_at, now()), "
                "  consent_data_at = COALESCE(consent_data_at, now()), "
                "  consent_sensitive_at = COALESCE(consent_sensitive_at, now()), "
                "  age_confirmed_at = COALESCE(age_confirmed_at, now()) "
                "WHERE id = :id AND deleted_at IS NULL RETURNING id"
            ),
            {
                "id": user_id,
                "name": data.name,
                "address_form": data.address_form,
                "partner_name": data.partner_name,
                "timezone": data.timezone,
            },
        )
        if updated.one_or_none() is None:
            return False
        await s.execute(
            text("INSERT INTO agents (user_id) VALUES (:id) ON CONFLICT (user_id) DO NOTHING"),
            {"id": user_id},
        )
    return True


async def record_underage(sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> None:
    """Возраст не подтверждён (ТЗ §9.3): доступ закрывается (снимается отметка 18+), событие
    фиксируется в safety_events только типом — без текста и личных данных. Согласия и профиль
    из этого запроса не сохраняются."""
    async with user_scope(sm, user_id) as s:
        await s.execute(
            text(
                "UPDATE users SET age_confirmed_at = NULL "
                "WHERE id = :id AND deleted_at IS NULL AND age_confirmed_at IS NOT NULL"
            ),
            {"id": user_id},
        )
        await s.execute(
            text("INSERT INTO safety_events (user_id, type) VALUES (:id, 'underage')"),
            {"id": user_id},
        )


async def get_me(sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> Me | None:
    async with user_scope(sm, user_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT id, name, address_form, partner_name, timezone, "
                    "consent_terms_at IS NOT NULL AND consent_data_at IS NOT NULL "
                    "  AND consent_sensitive_at IS NOT NULL AND age_confirmed_at IS NOT NULL "
                    "  AS onboarded, "
                    "EXISTS (SELECT 1 FROM couple_members cm JOIN couples c ON c.id = cm.couple_id "
                    "        WHERE cm.user_id = users.id AND cm.active AND c.status = 'active') "
                    "  AS has_couple "
                    "FROM users WHERE id = :id AND deleted_at IS NULL"
                ),
                {"id": user_id},
            )
        ).one_or_none()
    if row is None:
        return None
    return Me(
        id=row.id,
        name=row.name,
        address_form=row.address_form,
        partner_name=row.partner_name,
        timezone=row.timezone,
        onboarding_completed=row.onboarded,
        has_active_couple=row.has_couple,
    )

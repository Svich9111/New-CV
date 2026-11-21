"""Слой доступа к пользователям. Все запросы — внутри user_scope (RLS)."""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import user_scope
from app.schemas import Me


async def upsert_by_telegram_id(
    sm: async_sessionmaker[AsyncSession], telegram_id: int
) -> uuid.UUID | None:
    """Найти или создать пользователя. None — аккаунт удалён (повторная регистрация — 4.5)."""
    async with user_scope(sm, None) as s:
        row = await s.execute(text("SELECT app_upsert_user(:tg)"), {"tg": telegram_id})
        value = row.scalar_one()
        return uuid.UUID(str(value)) if value is not None else None


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

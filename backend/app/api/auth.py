"""ФТ-01: вход через Telegram по initData; GET /me — профиль текущего пользователя;
ФТ-02: PUT /me/onboarding — онбординг, согласия, 18+."""

from fastapi import APIRouter

from app.api.deps import AppSettings, CurrentUser, SessionMaker, unauthorized
from app.config import ConfigError, check_auth_config
from app.core.session import issue_token
from app.core.telegram import InitDataError, validate_init_data
from app.errors import AppError
from app.repositories import users
from app.schemas import Me, OnboardingRequest, Session, TelegramAuthRequest

router = APIRouter()


@router.post("/auth/telegram", tags=["auth"])
async def auth_telegram(
    body: TelegramAuthRequest, sm: SessionMaker, settings: AppSettings
) -> Session:
    try:
        check_auth_config(settings)
    except ConfigError as exc:
        raise AppError(503, "service_unavailable", "Вход временно недоступен") from exc
    try:
        tg = validate_init_data(
            body.init_data,
            settings.telegram_bot_token.get_secret_value(),
            settings.init_data_max_age_s,
        )
    except InitDataError as exc:
        raise AppError(
            401, "invalid_init_data", "Не удалось подтвердить вход через Telegram"
        ) from exc
    user_id = await users.upsert_by_telegram_id(sm, tg.id)
    if user_id is None:
        raise AppError(403, "account_deleted", "Аккаунт удалён")
    me = await users.get_me(sm, user_id)
    if me is None:
        raise unauthorized()
    token = issue_token(user_id, settings.session_secret.get_secret_value(), settings.session_ttl_s)
    return Session(access_token=token, expires_in=settings.session_ttl_s, me=me)


@router.get("/me", tags=["profile"])
async def get_me(user_id: CurrentUser, sm: SessionMaker) -> Me:
    me = await users.get_me(sm, user_id)
    if me is None:
        raise unauthorized()
    return me


@router.put("/me/onboarding", tags=["profile"])
async def put_onboarding(body: OnboardingRequest, user_id: CurrentUser, sm: SessionMaker) -> Me:
    if not body.age_confirmed:
        await users.record_underage(sm, user_id)
        raise AppError(403, "underage", "Сервис доступен только с 18 лет")
    if not await users.complete_onboarding(sm, user_id, body):
        raise unauthorized()
    me = await users.get_me(sm, user_id)
    if me is None:
        raise unauthorized()
    return me

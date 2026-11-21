"""ФТ-03: пара и одноразовая ссылка-приглашение на 7 дней; ФТ-04: одна активная пара, выход.

Все изменения — функциями БД через `repositories.couples` (АР-12, АР-13). Токен
приглашения не пишется в журналы и не уходит в БД: туда передаётся только его SHA-256."""

from fastapi import APIRouter, Response

from app.api.deps import AppSettings, CurrentUser, OnboardedUser, SessionMaker, unauthorized
from app.config import ConfigError, check_invite_config
from app.core.invites import hash_token, invite_link, is_well_formed, new_token
from app.errors import AppError
from app.repositories import couples
from app.repositories.couples import CoupleError
from app.schemas import Couple, InviteCreated, InvitePreview

router = APIRouter(tags=["couple"])


def not_found() -> AppError:
    # Один ответ на «нет», «нет доступа» и «неверный формат» — ничего не раскрывается.
    return AppError(404, "not_found", "Не найдено")


def _gone_expired() -> AppError:
    return AppError(410, "invite_expired", "Срок приглашения истёк — попросите новую ссылку")


def _gone_used() -> AppError:
    return AppError(410, "invite_used", "Приглашение уже использовано — попросите новую ссылку")


def _http_error(exc: CoupleError) -> AppError:
    """Коды функций пары (DECISIONS.md, АР-13) → ответы контракта."""
    match exc.sqlstate:
        case "FB001" | "FB007" | "22023":
            return not_found()
        case "FB002":
            return _gone_expired()
        case "FB003":
            return _gone_used()
        case "FB004":
            return AppError(409, "own_invite", "Это ваше приглашение — отправьте ссылку партнёру")
        case "FB005" | "23505":
            return AppError(409, "already_in_couple", "У вас уже есть пара")
        case "FB006":
            return AppError(409, "couple_not_pending", "Пара уже не ожидает партнёра")
        case "FB008":
            return AppError(403, "onboarding_required", "Сначала завершите знакомство с сервисом")
        case _:  # 42501: пользователь не найден или удалён
            return unauthorized()


def _token_hash(token: str) -> bytes:
    if not is_well_formed(token):
        raise not_found()
    return hash_token(token)


def _issued(settings: AppSettings) -> tuple[str, bytes]:
    try:
        check_invite_config(settings)
    except ConfigError as exc:
        raise AppError(503, "service_unavailable", "Приглашения временно недоступны") from exc
    token = new_token()
    return token, hash_token(token)


@router.get("/couple")
async def get_couple(user_id: OnboardedUser, sm: SessionMaker) -> Couple:
    couple = await couples.get_current(sm, user_id)
    if couple is None:
        raise not_found()
    return couple


@router.post("/couple", status_code=201)
async def create_couple(
    user_id: OnboardedUser, sm: SessionMaker, settings: AppSettings
) -> InviteCreated:
    token, token_hash = _issued(settings)
    try:
        issued = await couples.create_with_invite(sm, user_id, token_hash)
    except CoupleError as exc:
        raise _http_error(exc) from exc
    return InviteCreated(
        couple_id=issued.couple_id,
        link=invite_link(settings.bot_username, token),
        expires_at=issued.expires_at,
    )


@router.post("/couple/invite", status_code=201)
async def reissue_invite(
    user_id: OnboardedUser, sm: SessionMaker, settings: AppSettings
) -> InviteCreated:
    token, token_hash = _issued(settings)
    try:
        issued = await couples.issue_invite(sm, user_id, token_hash)
    except CoupleError as exc:
        raise _http_error(exc) from exc
    return InviteCreated(
        couple_id=issued.couple_id,
        link=invite_link(settings.bot_username, token),
        expires_at=issued.expires_at,
    )


@router.post("/couple/leave", status_code=204)
async def leave_couple(user_id: OnboardedUser, sm: SessionMaker) -> Response:
    try:
        await couples.leave(sm, user_id)
    except CoupleError as exc:
        raise _http_error(exc) from exc
    return Response(status_code=204)


@router.get("/invites/{token}")
async def view_invite(token: str, user_id: CurrentUser, sm: SessionMaker) -> InvitePreview:
    """Доступно после входа, но до онбординга: показать, кто пригласил (ТЗ §4.1 п.4)."""
    invite = await couples.find_invite(sm, user_id, _token_hash(token))
    if invite is None:
        raise not_found()
    if invite.status in ("used", "revoked"):
        raise _gone_used()
    if invite.expired:
        raise _gone_expired()
    return InvitePreview(inviter_name=invite.inviter_name, expires_at=invite.expires_at)


@router.post("/invites/{token}/accept")
async def accept_invite(token: str, user_id: OnboardedUser, sm: SessionMaker) -> Couple:
    token_hash = _token_hash(token)
    try:
        return await couples.accept_invite(sm, user_id, token_hash)
    except CoupleError as exc:
        raise _http_error(exc) from exc

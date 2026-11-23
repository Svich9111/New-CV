"""ФТ-21: удаление аккаунта (`DELETE /me`) и выгрузка своих данных (`GET /me/export`).

Оба доступны после входа без завершённого онбординга (АР-17, АР-25): удалить и выгрузить свои
данные можно всегда. Вся работа с БД — `repositories.account` (функции `app_delete_account`,
`app_export_partner_messages` и чтение под RLS). 40P01/40001 → 409 `conflict_retry` — общий
обработчик (`app.errors`, АР-24)."""

from fastapi import APIRouter, Response

from app.api.deps import CurrentUser, Keys, SessionMaker, unauthorized
from app.repositories import account
from app.repositories.account import AccountGone
from app.schemas import AccountExport

router = APIRouter(tags=["profile"])


@router.delete("/me", status_code=204)
async def delete_me(user_id: CurrentUser, sm: SessionMaker) -> Response:
    """После 204 строки пользователя нет: `current_user_id` на тот же токен отвечает 401."""
    try:
        await account.delete_account(sm, user_id)
    except AccountGone as exc:
        raise unauthorized() from exc
    return Response(status_code=204)


@router.get("/me/export")
async def export_me(
    user_id: CurrentUser, sm: SessionMaker, keys: Keys, response: Response
) -> AccountExport:
    try:
        data = await account.export_account(sm, keys, user_id)
    except AccountGone as exc:
        raise unauthorized() from exc
    # Личные данные не сохраняются ни клиентом, ни промежуточными узлами (контракт).
    response.headers["Cache-Control"] = "no-store"
    return data

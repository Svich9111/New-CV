"""«Что агент помнит» — `GET /memory`, `DELETE /memory/{memory_id}` (ФТ-13; ТЗ §2 п.6, §6.3;
задача 2.4, этап 2; АР-36).

Доступно после входа без завершённого онбординга (как выгрузка и удаление аккаунта, АР-17,
АР-25): принцип 6 «память под контролем» — человек, снявший согласие, всё равно видит и удаляет
свои записи; код `onboarding_required` эти пути не отдают (в контракте 403 не объявлен). Без
агента (онбординг не пройден) записей нет — пустой список.

Только свои записи — `repositories.memory` (RLS + явный отбор по владельцу). Удаление — функцией
БД `app_delete_memory`: чужая, удалённая и несуществующая запись — одинаково 404, повтор — 404.
Запись, текст которой не расшифровался, в списке не показывается: `MemoryItem.text` в контракте —
только строка (`UnavailableText` не допускается; карточка A1 в отчёте 2.4 э.2); в журнал — только
код причины и колонка (`texts.open_row`). Ответы не кэшируются (`Cache-Control: no-store`)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api.deps import CurrentUser, Keys, SessionMaker, unauthorized
from app.errors import not_found
from app.repositories import memory
from app.repositories.memory import MemoryOwnerGone
from app.schemas import MemoryItem, MemoryPage, MemoryTypeName

router = APIRouter(tags=["memory"])


@router.get("/memory")
async def list_memory(
    user_id: CurrentUser,
    sm: SessionMaker,
    keys: Keys,
    response: Response,
    memory_type: Annotated[MemoryTypeName | None, Query(alias="type")] = None,
) -> MemoryPage:
    rows = await memory.list_memory(sm, keys, user_id, memory_type=memory_type)
    response.headers["Cache-Control"] = "no-store"
    return MemoryPage(
        items=[
            MemoryItem(
                id=r.id,
                type=r.type,
                text=r.text,
                event_date=r.event_date,
                source=r.source,
                confidence=r.confidence,
                created_at=r.created_at,
            )
            for r in rows
            if r.text is not None
        ]
    )


@router.delete("/memory/{memory_id}", status_code=204)
async def delete_memory(user_id: CurrentUser, sm: SessionMaker, memory_id: str) -> Response:
    try:
        item = uuid.UUID(memory_id)
    except ValueError as exc:
        raise not_found() from exc  # неверный формат — тот же 404, ничего не раскрывается
    try:
        deleted = await memory.delete_memory(sm, user_id, item)
    except MemoryOwnerGone as exc:
        raise unauthorized() from exc
    if not deleted:
        raise not_found()
    return Response(status_code=204, headers={"Cache-Control": "no-store"})

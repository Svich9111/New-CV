"""«Что агент помнит» — `GET /memory`, `DELETE /memory/{memory_id}` (ФТ-13; ТЗ §2 п.6, §6.3;
задача 2.4, этап 2; АР-36).

Доступно после входа без завершённого онбординга (как выгрузка и удаление аккаунта, АР-17,
АР-25): принцип 6 «память под контролем» — человек, снявший согласие, всё равно видит и удаляет
свои записи; код `onboarding_required` эти пути не отдают (в контракте 403 не объявлен). Без
агента (онбординг не пройден) записей нет — пустой список.

Только свои записи — `repositories.memory` (RLS + явный отбор по владельцу). Удаление —
`ai.memory.forget_memory` (АР-37: `app_delete_memory` + очистка сводок): чужая, удалённая и
несуществующая запись — одинаково 404, повтор — 404.
Запись, текст которой не расшифровался, показывается с `UnavailableText` (только код причины) —
её видно и можно удалить (контракт `MemoryItem.text` — `ExportText`); в журнал — только код
причины и колонка (`texts.open_row`). Ответы не кэшируются (`Cache-Control: no-store`)."""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import memory as ai_memory
from app.ai.memory import ForgetJob
from app.ai.models import AgentModels
from app.api.chat import ChatRuntime
from app.api.deps import CurrentUser, Keys, SessionMaker
from app.core.crypto import Keyring
from app.errors import not_found
from app.repositories import memory
from app.schemas import MemoryItem, MemoryPage, MemoryTypeName, UnavailableText

router = APIRouter(tags=["memory"])
logger = logging.getLogger(__name__)


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
                text=r.text if r.text is not None else UnavailableText(reason=r.unavailable),
                event_date=r.event_date,
                source=r.source,
                confidence=r.confidence,
                created_at=r.created_at,
            )
            for r in rows
        ]
    )


@router.delete("/memory/{memory_id}", status_code=204)
async def delete_memory(
    request: Request, user_id: CurrentUser, sm: SessionMaker, keys: Keys, memory_id: str
) -> Response:
    """АР-37: удаление и пометка сводок «на очистку» — одной транзакцией (``forget_memory``);
    204 не ждёт очистки — её делает фоновая ``run_forget``. Пока она не прошла (или если не
    прошла: сбой, перезапуск, моделей нет), помеченные сводки в контекст не идут, а сжатие
    памяти их обнулит — удалённое не возвращается ни в каком случае."""
    try:
        item = uuid.UUID(memory_id)
    except ValueError as exc:
        raise not_found() from exc  # неверный формат — тот же 404, ничего не раскрывается
    job = await ai_memory.forget_memory(sm, keys, user_id, item)
    if job is None:
        raise not_found()  # чужая, удалённая, несуществующая — неразличимо
    _forget_later(request, sm, keys, job)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


def _forget_later(
    request: Request, sm: async_sessionmaker[AsyncSession], keys: Keyring, job: ForgetJob
) -> None:
    """Фоновая очистка сводок (``run_forget``) — задачей ``ChatRuntime`` (ссылка удерживается;
    при остановке сервиса снимается вместе со сжатием — сводки остаются скрытыми до сжатия
    памяти, АР-37). Не запускается: моделей ещё нет (создаются при первом ответе чата; сеть при
    удалении не нужна) или текст записи не расшифровался (переписать «без него» нельзя — сводки
    остаются скрытыми до сжатия). Текст задачи в журнал не пишется."""
    rt: ChatRuntime | None = getattr(request.app.state, "chat", None)
    if rt is None or rt.models is None:
        logger.info("memory forget: cleanup deferred to compaction (models not ready)")
        return
    if not job.text:
        logger.info("memory forget: cleanup deferred to compaction (text unavailable)")
        return
    rt.spawn(_run_forget(sm, keys, rt.models, job))


async def _run_forget(
    sm: async_sessionmaker[AsyncSession], keys: Keyring, models: AgentModels, job: ForgetJob
) -> None:
    try:
        out = await ai_memory.run_forget(sm, keys, models, job)
    except Exception as exc:
        logger.error("memory forget: failed: %s", type(exc).__name__)
        return
    logger.info("memory forget: status=%s cleaned=%s error=%s", out.status, out.merged, out.error)

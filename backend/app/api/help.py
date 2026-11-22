"""Контакты служб помощи — `GET /help/contacts` (ФТ-17; ТЗ §9.3, §11 п.5; АР-33).

Без подписки и без онбординга: помощь доступна всегда, кто бы ни вошёл. Контракт требует входа
(общая схема `bearer`), поэтому без сессии — 401 (в контракте у пути не объявлен — карточка для
A1 в отчёте 2.1). Источник — `app/help_contacts.json` (`app.help`), БД не нужна."""

from fastapi import APIRouter, Response

from app.api.deps import CurrentUser
from app.help import help_contacts
from app.schemas import HelpContact

router = APIRouter(tags=["help"])


@router.get("/help/contacts", response_model=list[HelpContact], response_model_exclude_none=True)
async def get_help_contacts(_: CurrentUser, response: Response) -> list[HelpContact]:
    """Ответ проверяется схемой `HelpContact` (ревью MINOR-9); `round_the_clock` без значения
    не выводится (в контракте поле не допускает null)."""
    response.headers["Cache-Control"] = "no-store"
    return [HelpContact(**c) for c in help_contacts()]

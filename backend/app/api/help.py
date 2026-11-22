"""Контакты служб помощи — `GET /help/contacts` (ФТ-17; ТЗ §9.3, §11 п.5; АР-33).

Публичный путь (контракт `security: []`, Б-58, задача 2.0v): без сессии, без онбординга и без
подписки — помощь открывается и с истёкшей сессией, и у пользователя, которому отказано по
возрасту (§9.3). Заголовок `Authorization` не читается вовсе (негодный токен — тоже 200), в
ответе нет данных пользователя. Источник — `app/help_contacts.json` (`app.help`), БД не нужна.
`Cache-Control: no-store` оставлен: список меняет оператор (проверка до запуска, АР-33), и
устаревшая копия в кешах клиента или прокси не должна пережить правку номеров."""

from fastapi import APIRouter, Response

from app.help import help_contacts
from app.schemas import HelpContact

router = APIRouter(tags=["help"])


@router.get("/help/contacts", response_model=list[HelpContact], response_model_exclude_none=True)
async def get_help_contacts(response: Response) -> list[HelpContact]:
    """Ответ проверяется схемой `HelpContact` (ревью MINOR-9); `round_the_clock` без значения
    не выводится (в контракте поле не допускает null)."""
    response.headers["Cache-Control"] = "no-store"
    return [HelpContact(**c) for c in help_contacts()]

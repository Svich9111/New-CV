"""Каркас уведомлений через бота (ФТ-18; сами уведомления — задача 3.5).

Уведомление не раскрывает текст сообщения (ТЗ §4.4 п.3): функция принимает только вид
уведомления, текст берётся из фиксированных шаблонов. Передать сюда текст сообщения пары
нельзя по сигнатуре; не добавляйте параметров со свободным текстом."""

from enum import StrEnum

from app.bot.client import BotApi, webapp_keyboard


class NotificationKind(StrEnum):
    NEW_MESSAGE = "new_message"
    PARTNER_JOINED = "partner_joined"


_TEXTS = {
    NotificationKind.NEW_MESSAGE: "Партнёр прислал вам сообщение. Откройте приложение, чтобы его "
    "прочитать.",
    NotificationKind.PARTNER_JOINED: "Партнёр принял приглашение — пара создана.",
}
_BUTTON = "Открыть"


async def notify(api: BotApi, chat_id: int, kind: NotificationKind, webapp_url: str) -> None:
    await api.send_message(chat_id, _TEXTS[kind], webapp_keyboard(_BUTTON, webapp_url))

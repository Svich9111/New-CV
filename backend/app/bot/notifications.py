"""Каркас уведомлений через бота (ФТ-18; сами уведомления — задача 3.5).

Уведомление не раскрывает текст сообщения (ТЗ §4.4 п.3): функция принимает только вид
уведомления, текст берётся из фиксированных шаблонов. Передать сюда текст сообщения пары
нельзя по сигнатуре; не добавляйте параметров со свободным текстом."""

import uuid
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.client import BotApi, webapp_keyboard
from app.repositories import users


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


async def notify_user(
    api: BotApi,
    sm: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    kind: NotificationKind,
    webapp_url: str,
) -> bool:
    """Уведомление пользователю по его id: чат Telegram берётся в момент отправки, а не при
    постановке в очередь. Получатель удалил аккаунт (ФТ-21, АР-23) — его строки нет, ничего не
    отправляется и в журнал ничего не пишется (это штатный исход, не ошибка). → отправлено ли."""
    chat_id = await users.notification_chat_id(sm, user_id)
    if chat_id is None:
        return False
    await notify(api, chat_id, kind, webapp_url)
    return True

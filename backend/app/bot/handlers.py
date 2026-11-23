"""Обработка обновлений Telegram. Текст сообщений и payload команды /start не сохраняются,
не журналируются и не возвращаются в ответе (ТЗ §9.1 п.5).

Для ссылок `t.me/<бот>?startapp=<токен>` (ФТ-03) Telegram открывает Mini App сам; бот лишь
отвечает на /start кнопкой открытия Mini App."""

import logging
from typing import Any

from app.bot.client import BotApi, webapp_keyboard

log = logging.getLogger("app.bot")

BUTTON_TEXT = "Открыть «Чувства AI»"
START_TEXT = (
    "Здравствуйте! «Чувства AI» помогает паре слышать друг друга: у каждого — свой агент, "
    "который поможет подобрать слова. Откройте приложение кнопкой ниже."
)
HINT_TEXT = "Разговор с агентом и сообщения партнёру — в приложении. Откройте его кнопкой ниже."

_START_COMMANDS = {"/start", "/help"}


def _command(text: str) -> str | None:
    """Имя команды без payload и без «@имя_бота»; None — это не команда."""
    if not text.startswith("/"):
        return None
    head = text.split(maxsplit=1)[0] if text.strip() else text
    return head.split("@", 1)[0].lower()


def _private_chat_id(message: dict[str, Any]) -> int | None:
    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("type") != "private":
        return None
    chat_id = chat.get("id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return None
    return chat_id


_KNOWN_KINDS = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "callback_query",
    "inline_query",
    "chosen_inline_result",
    "shipping_query",
    "pre_checkout_query",
    "poll",
    "poll_answer",
    "my_chat_member",
    "chat_member",
    "chat_join_request",
)


def update_kind(update: dict[str, Any]) -> str:
    """Вид обновления для журнала — только имя из фиксированного списка, иначе «other»."""
    for kind in _KNOWN_KINDS:
        if kind in update:
            return kind
    return "other"


async def handle_update(api: BotApi, update: dict[str, Any], webapp_url: str) -> str:
    """Обрабатывает одно обновление; возвращает метку исхода для журнала (без текстов).
    Ошибки Bot API (`BotApiError`) пробрасываются — решение о повторе принимает вебхук."""
    message = update.get("message")
    if not isinstance(message, dict):
        return "ignored"
    chat_id = _private_chat_id(message)
    if chat_id is None:
        return "ignored"
    text = message.get("text")
    if not isinstance(text, str):
        return "ignored"
    command = _command(text)
    keyboard = webapp_keyboard(BUTTON_TEXT, webapp_url)
    if command in _START_COMMANDS:
        await api.send_message(chat_id, START_TEXT, keyboard)
        return command.removeprefix("/")
    # Неизвестная команда или обычный текст: не повторяем его, лишь показываем путь в приложение.
    await api.send_message(chat_id, HINT_TEXT, keyboard)
    return "hint"

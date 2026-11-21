"""Настройки приложения. Все секреты — только из переменных окружения (CLAUDE.md)."""

import re
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FEELINGS_", extra="ignore")

    env: str = "dev"
    # Приложение — только под ролью входа feelings_api (АР-10); пароль задаётся окружением.
    database_url: str = "postgresql+asyncpg://feelings_api@localhost:5432/feelings"
    # Владелец схемы для Alembic; приложение под ним не работает.
    migration_database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/feelings"
    redis_url: str = "redis://localhost:6379/0"
    telegram_bot_token: SecretStr = SecretStr("")
    # Максимальный возраст initData, секунд (ФТ-01)
    init_data_max_age_s: int = 86400
    session_secret: SecretStr = SecretStr("")
    # Ключ шифрования текстов при хранении (ТЗ §9.1 п.3, задача 4.2)
    encryption_key: SecretStr = SecretStr("")
    session_ttl_s: int = 3600
    # Имя бота без «@» для ссылки-приглашения https://t.me/<бот>?startapp=<токен> (ФТ-03).
    # Не секрет; правила имени Telegram — 5–32 символа, латиница, цифры, «_».
    bot_username: str = Field(default="", pattern=r"^([A-Za-z][A-Za-z0-9_]{4,31})?$")
    # Секрет вебхука: Telegram присылает его в X-Telegram-Bot-Api-Secret-Token (задача 1.4).
    # Правила Telegram — 1–256 символов A-Z a-z 0-9 _ -; требуем не короче 32.
    telegram_webhook_secret: SecretStr = SecretStr("")
    # Адрес Mini App (https) для кнопки web_app в ответе на /start и кнопки меню (задача 1.4).
    webapp_url: str = ""


MIN_SESSION_SECRET_LEN = 32


class ConfigError(Exception):
    pass


def check_auth_config(settings: Settings) -> None:
    """Без токена бота и стойкого секрета сессии вход невозможен — отказ до любых записей в БД."""
    if not settings.telegram_bot_token.get_secret_value():
        raise ConfigError("FEELINGS_TELEGRAM_BOT_TOKEN is empty")
    if len(settings.session_secret.get_secret_value()) < MIN_SESSION_SECRET_LEN:
        raise ConfigError(
            f"FEELINGS_SESSION_SECRET must be at least {MIN_SESSION_SECRET_LEN} characters"
        )


def check_invite_config(settings: Settings) -> None:
    """Без имени бота ссылку-приглашение не собрать (ФТ-03)."""
    if not settings.bot_username:
        raise ConfigError("FEELINGS_BOT_USERNAME is empty")


_WEBHOOK_SECRET = re.compile(r"^[A-Za-z0-9_-]{32,256}$")


def _check_bot_token(settings: Settings) -> None:
    if not settings.telegram_bot_token.get_secret_value():
        raise ConfigError("FEELINGS_TELEGRAM_BOT_TOKEN is empty")


def _check_webapp_url(settings: Settings) -> None:
    url = urlsplit(settings.webapp_url)
    if url.scheme != "https" or not url.hostname:
        raise ConfigError("FEELINGS_WEBAPP_URL must be an https:// URL")


def check_bot_config(settings: Settings) -> None:
    """Без токена, стойкого секрета вебхука и адреса Mini App бот не работает (задача 1.4).
    Значения секретов в текст ошибки не попадают."""
    _check_bot_token(settings)
    if not _WEBHOOK_SECRET.fullmatch(settings.telegram_webhook_secret.get_secret_value()):
        raise ConfigError(
            "FEELINGS_TELEGRAM_WEBHOOK_SECRET must be 32-256 characters of A-Z a-z 0-9 _ -"
        )
    _check_webapp_url(settings)


def check_bot_token_config(settings: Settings) -> None:
    """Для проверки токена (`python -m app.bot.check`) нужен только токен бота."""
    _check_bot_token(settings)


def check_dev_bot_config(settings: Settings) -> None:
    """Бот без сервера: опрос getUpdates (`python -m app.bot.poll`, только разработка) и
    настройка без вебхука (`python -m app.bot.setup --no-webhook`). Нужны токен и адрес
    Mini App (https) для кнопки web_app; секрет вебхука и БД не нужны. Проверку сервера
    (`check_bot_config`) не ослабляет."""
    _check_bot_token(settings)
    _check_webapp_url(settings)


@lru_cache
def get_settings() -> Settings:
    return Settings()

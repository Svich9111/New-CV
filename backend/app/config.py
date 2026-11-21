"""Настройки приложения. Все секреты — только из переменных окружения (CLAUDE.md)."""

from functools import lru_cache

from pydantic import SecretStr
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


@lru_cache
def get_settings() -> Settings:
    return Settings()

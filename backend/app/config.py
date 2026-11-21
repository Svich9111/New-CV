"""Настройки приложения. Все секреты — только из переменных окружения (CLAUDE.md)."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FEELINGS_", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/feelings"
    redis_url: str = "redis://localhost:6379/0"
    telegram_bot_token: SecretStr = SecretStr("")
    # Максимальный возраст initData, секунд (ФТ-01)
    init_data_max_age_s: int = 86400
    session_secret: SecretStr = SecretStr("")
    # Ключ шифрования текстов при хранении (ТЗ §9.1 п.3, задача 4.2)
    encryption_key: SecretStr = SecretStr("")
    session_ttl_s: int = 3600


@lru_cache
def get_settings() -> Settings:
    return Settings()

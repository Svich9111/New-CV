"""Настройки приложения. Все секреты — только из переменных окружения (CLAUDE.md)."""

import re
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FEELINGS_", extra="ignore")

    # По умолчанию — продуктив (ревью 2.1 э.2 MINOR-4): сервер без явного FEELINGS_ENV
    # выполняет все проверки продуктива (АР-33 — проверенные контакты помощи) и не даёт
    # запустить опрос бота; разработка и тесты задают dev/test/local явно (.env.example).
    env: str = "production"
    # Приложение — только под ролью входа feelings_api (АР-10); пароль задаётся окружением.
    database_url: str = "postgresql+asyncpg://feelings_api@localhost:5432/feelings"
    # Владелец схемы для Alembic; приложение под ним не работает.
    migration_database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/feelings"
    redis_url: str = "redis://localhost:6379/0"
    telegram_bot_token: SecretStr = SecretStr("")
    # Максимальный возраст initData, секунд (ФТ-01)
    init_data_max_age_s: int = 86400
    session_secret: SecretStr = SecretStr("")
    # Ключи шифрования текстов при хранении (ТЗ §9.1 п.3, задача 4.2): набор
    # «id:base64url(32 байта)» через запятую и id текущего; проверка — app.core.crypto.
    encryption_keys: SecretStr = SecretStr("")
    encryption_key_id: str = ""
    # Ключ отпечатков записей памяти (задача 2.4, АР-36): base64url, не короче 32 байт; прежние
    # ключи — через запятую (только для проверки удалённых). Проверка — app.core.fingerprint.
    memory_fingerprint_key: SecretStr = SecretStr("")
    memory_fingerprint_keys_old: SecretStr = SecretStr("")
    # Воркер памяти (2.4): сессия завершена после паузы (Б-54, АР-36); аренда; сжатие (§6.3 п.3).
    memory_session_gap_min: int = Field(default=30, ge=1, le=7 * 24 * 60)
    memory_lease_min: int = Field(default=10, ge=1, le=24 * 60)
    memory_compact_every_h: int = Field(default=168, ge=1)
    memory_max_attempts: int = Field(default=5, ge=1)
    # Роль входа фонового воркера: член feelings_worker и feelings_app (NOINHERIT, Б-69).
    worker_database_url: str = ""
    session_ttl_s: int = 3600
    # Имя бота без «@» для ссылки-приглашения https://t.me/<бот>?startapp=<токен> (ФТ-03).
    # Не секрет; правила имени Telegram — 5–32 символа, латиница, цифры, «_».
    bot_username: str = Field(default="", pattern=r"^([A-Za-z][A-Za-z0-9_]{4,31})?$")
    # Секрет вебхука: Telegram присылает его в X-Telegram-Bot-Api-Secret-Token (задача 1.4).
    # Правила Telegram — 1–256 символов A-Z a-z 0-9 _ -; требуем не короче 32.
    telegram_webhook_secret: SecretStr = SecretStr("")
    # Адрес Mini App (https) для кнопки web_app в ответе на /start и кнопки меню (задача 1.4).
    webapp_url: str = ""
    # Личный чат (задача 2.1, этап 2). Дневной лимит реплик агенту на пользователя (ТЗ §10):
    # окно — календарные сутки UTC, счётчик — в Redis (`redis_url`); кризисные реплики
    # засчитываются, но не отклоняются (ТЗ §9.3).
    chat_daily_limit: int = Field(default=100, ge=1)
    # Строка-комментарий SSE для поддержания соединения, если событий нет дольше (секунды).
    chat_keepalive_s: float = Field(default=15.0, gt=0)
    # Модели OpenRouter по ролям (гипотеза АР-19); ключ — OPENROUTER_API_KEY (не FEELINGS_).
    llm_writer_model: str = "anthropic/claude-sonnet-5.5"
    llm_classifier_model: str = "anthropic/claude-haiku-5.5"
    # Админ-API базы знаний (задача 2.6; АР-39). Только SHA-256 (hex) действующих токенов
    # администратора, через запятую — для ротации; пусто — админ-API закрыт (всегда 401).
    admin_token_sha256: str = ""
    # Подключение администратора: роль входа — член feelings_admin (Б-75), не feelings_api.
    admin_database_url: str = ""
    # Предел тела запроса на уровне сервера (байты): тело больше не читается целиком в память.
    # Загрузка материала базы знаний — свой предел (20 МиБ файла + поля формы, app.api.admin_kb).
    max_request_body_bytes: int = Field(default=1_048_576, ge=1024)


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


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def admin_token_hashes(settings: Settings) -> tuple[bytes, ...]:
    """SHA-256 действующих токенов администратора (байты). Неверная запись — ``ConfigError``
    (значение в текст ошибки не попадает)."""
    out: list[bytes] = []
    for raw in settings.admin_token_sha256.split(","):
        item = raw.strip().lower()
        if not item:
            continue
        if not _SHA256_HEX.fullmatch(item):
            raise ConfigError("FEELINGS_ADMIN_TOKEN_SHA256 must be comma-separated SHA-256 hex")
        out.append(bytes.fromhex(item))
    return tuple(out)


def check_admin_config(settings: Settings) -> None:
    """Токены администратора заданы — нужно и подключение администратора (Б-75)."""
    if admin_token_hashes(settings) and not settings.admin_database_url:
        raise ConfigError("FEELINGS_ADMIN_DATABASE_URL is empty while admin tokens are set")


@lru_cache
def get_settings() -> Settings:
    return Settings()

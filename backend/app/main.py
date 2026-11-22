"""Точка входа FastAPI."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import account, admin_kb, auth, chat, couple, couple_messages, help, memory
from app.bot import webhook as bot_webhook
from app.bot.client import BotApi
from app.bot.dedup import UpdateDeduplicator
from app.config import (
    check_admin_config,
    check_auth_config,
    check_bot_config,
    check_invite_config,
    get_settings,
)
from app.core.body_limit import BodyLimit
from app.core.crypto import load_keyring
from app.core.limits import RedisDailyLimit
from app.core.logging import install_log_redaction
from app.db import make_engine, make_sessionmaker
from app.errors import install_error_handlers
from app.help import check_help_contacts_config


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if getattr(app.state, "sessionmaker", None) is None:
        settings = get_settings()
        check_auth_config(settings)  # сервис не стартует с пустыми ключами
        check_invite_config(settings)  # и без имени бота для ссылок-приглашений
        check_bot_config(settings)  # и без секрета вебхука и адреса Mini App (задача 1.4)
        check_admin_config(settings)  # и с токенами администратора без его подключения (2.6)
        check_help_contacts_config(settings)  # и в продуктив без проверенных контактов (АР-33)
        # и без ключей шифрования текстов или с ключом неверной длины (задача 4.2)
        app.state.keyring = load_keyring(settings)
        engine = make_engine(settings.database_url)
        app.state.sessionmaker = make_sessionmaker(engine)
        # Админ-API базы знаний — отдельное подключение (роль входа — член feelings_admin, Б-75).
        admin_engine = (
            make_engine(settings.admin_database_url) if settings.admin_database_url else None
        )
        app.state.admin_sessionmaker = make_sessionmaker(admin_engine) if admin_engine else None
        limiter = RedisDailyLimit.from_url(settings.redis_url)  # дневной лимит чата (2.1)
        app.state.chat = chat.ChatRuntime(limiter=limiter)
        try:
            app.state.bot_api = BotApi(settings.telegram_bot_token)
            try:
                yield
            finally:
                await app.state.bot_api.aclose()
        finally:
            await app.state.chat.aclose()  # фоновое сжатие истории снимается
            await limiter.aclose()
            if admin_engine is not None:
                await admin_engine.dispose()
            await engine.dispose()
    else:
        yield


# Токен приглашения в пути (/invites/<токен>) не попадает в журнал доступа uvicorn,
# токен бота из URL Bot API — в журнал httpx.
install_log_redaction()

app = FastAPI(title="Чувства AI", version="0.1.0", lifespan=lifespan)
app.state.sessionmaker = None
app.state.bot_api = None  # клиент Bot API создаётся при старте (lifespan)
app.state.keyring = None  # набор ключей шифрования создаётся при старте (lifespan)
# Модели, лимит и фоновые задачи личного чата; при старте — с лимитом в Redis (lifespan).
app.state.chat = chat.ChatRuntime()
app.state.admin_sessionmaker = None  # подключение администратора (lifespan; тесты задают своё)
app.state.bot_updates = UpdateDeduplicator()  # идемпотентность по update_id (в памяти)
install_error_handlers(app)
WEBHOOK_MAX_BODY_BYTES = 16 * 1024 * 1024
# Предел тела запроса на уровне сервера (2.6): по умолчанию — FEELINGS_MAX_REQUEST_BODY_BYTES,
# загрузка материала базы знаний — 20 МиБ файла + поля формы.
app.add_middleware(
    BodyLimit,
    default=lambda: get_settings().max_request_body_bytes,
    overrides={
        ("POST", admin_kb.UPLOAD_PATH): (
            admin_kb.MAX_UPLOAD_BODY_BYTES,
            "file_too_large",
            "Файл больше 20 МиБ",
        ),
        # Вебхук Telegram: на ошибку Telegram повторяет доставку — неправдоподобно большое
        # обновление получает 200 «ignored» от обработчика (тест A8), предел — с запасом.
        ("POST", "/api/v1" + bot_webhook.WEBHOOK_PATH): (
            WEBHOOK_MAX_BODY_BYTES,
            "payload_too_large",
            "Запрос слишком большой",
        ),
    },
)
app.include_router(auth.router, prefix="/api/v1")
app.include_router(couple.router, prefix="/api/v1")
app.include_router(couple_messages.router, prefix="/api/v1")
app.include_router(account.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
app.include_router(help.router, prefix="/api/v1")
app.include_router(memory.router, prefix="/api/v1")
app.include_router(admin_kb.router, prefix="/api/v1")
# Служебный маршрут для Telegram, вне контракта клиента (tests/test_contract.py, SERVICE_ROUTES).
app.include_router(bot_webhook.router, prefix="/api/v1")


@app.get("/api/v1/health", tags=["service"])
async def health() -> dict[str, str]:
    return {"status": "ok"}

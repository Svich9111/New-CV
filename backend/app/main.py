"""Точка входа FastAPI."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import auth
from app.config import check_auth_config, get_settings
from app.db import make_engine, make_sessionmaker
from app.errors import install_error_handlers


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if getattr(app.state, "sessionmaker", None) is None:
        settings = get_settings()
        check_auth_config(settings)  # сервис не стартует с пустыми ключами
        engine = make_engine(settings.database_url)
        app.state.sessionmaker = make_sessionmaker(engine)
        yield
        await engine.dispose()
    else:
        yield


app = FastAPI(title="Чувства AI", version="0.1.0", lifespan=lifespan)
app.state.sessionmaker = None
install_error_handlers(app)
app.include_router(auth.router, prefix="/api/v1")


@app.get("/api/v1/health", tags=["service"])
async def health() -> dict[str, str]:
    return {"status": "ok"}

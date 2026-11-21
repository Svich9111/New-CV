"""Точка входа FastAPI."""

from fastapi import FastAPI

app = FastAPI(title="Чувства AI", version="0.1.0")


@app.get("/api/v1/health", tags=["service"])
async def health() -> dict[str, str]:
    return {"status": "ok"}

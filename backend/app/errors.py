"""Ошибки API в формате контракта: {code, message} (api/openapi.yaml, Error)."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

NOT_FOUND_CODE = "not_found"
NOT_FOUND_MESSAGE = "Не найдено"


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code, self.message = status, code, message


def not_found() -> AppError:
    """Один ответ на «нет», «нет доступа» и «неверный формат» — ничего не раскрывается."""
    return AppError(404, NOT_FOUND_CODE, NOT_FOUND_MESSAGE)


# Ответы маршрутизатора Starlette (нет маршрута, метод не тот) — тоже по схеме Error.
_HTTP_CODES = {
    404: (NOT_FOUND_CODE, NOT_FOUND_MESSAGE),
    405: ("method_not_allowed", "Метод не поддерживается"),
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"code": exc.code, "message": exc.message}
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, __: RequestValidationError) -> JSONResponse:
        # Тело запроса в ответ не возвращается: в нём может быть личный текст.
        return JSONResponse(
            status_code=422, content={"code": "validation_error", "message": "Проверьте данные"}
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Путь запроса (в нём может быть токен приглашения) в ответ не возвращается.
        code, message = _HTTP_CODES.get(exc.status_code, ("http_error", "Запрос не выполнен"))
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": code, "message": message},
            headers=exc.headers,
        )

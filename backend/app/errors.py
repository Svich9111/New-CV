"""Ошибки API в формате контракта: {code, message} (api/openapi.yaml, Error)."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.db import RETRYABLE_SQLSTATES, sqlstate

NOT_FOUND_CODE = "not_found"
NOT_FOUND_MESSAGE = "Не найдено"
INTERNAL_MESSAGE = "Что-то пошло не так — попробуйте ещё раз"


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

    @app.exception_handler(DBAPIError)
    async def _db_conflict(_: Request, exc: DBAPIError) -> JSONResponse:
        # АР-24: 40P01/40001 — транзакция отменена целиком, повтор безопасен: 409, не 500.
        # Ни SQL, ни параметров, ни текста драйвера в ответе нет. Прочие ошибки БД — как были
        # (необработанное исключение → 500).
        if sqlstate(exc) not in RETRYABLE_SQLSTATES:
            raise exc
        return JSONResponse(
            status_code=409,
            content={
                "code": "conflict_retry",
                "message": "Не получилось с первого раза — повторите, пожалуйста",
            },
            # Повтор — новым запросом: ответ не кэшируется (в т.ч. у /me/export, тест 4.5 О-9).
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, __: Exception) -> JSONResponse:
        # Необработанная ошибка — 500 по схеме Error (ревью 2.1 э.2 р.2 MINOR-2): без текста
        # исключения, SQL и данных запроса. Starlette после ответа пробрасывает исключение
        # дальше (журнал сервера, тесты с raise_app_exceptions).
        return JSONResponse(
            status_code=500,
            content={"code": "internal", "message": INTERNAL_MESSAGE},
            headers={"Cache-Control": "no-store"},
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

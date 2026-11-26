"""Запуск процесса воркера: ошибка настройки → код выхода 78, временная недоступность → повтор в
процессе (Б-90, тест 0.8 р.3 О-9; связано с Б-85).

Раньше любая ошибка при старте (нет ключа и кратковременный сбой сети до OpenRouter одинаково)
завершала процесс с кодом 1, и Docker перезапускал контейнер бесконечно: по ``dc ps`` нельзя было
отличить «исправьте ``.env``» от «подождите». Теперь:

- **ошибка настройки** (``ConfigError`` — нет/неверная переменная, ключи; неверные значения
  настроек; модель отклонила ключ, модели нет в каталоге; БД отвергла роль входа: пароль, база,
  права — SQLSTATE 28P01/28000/3D000/42501) — сообщение уровня CRITICAL **без значений секретов**
  (имя переменной или вид ошибки) и выход с кодом **78** (``EX_CONFIG``, sysexits.h).
  ``dc ps`` показывает ``Restarting (78)``, ``docker inspect`` — ``ExitCode 78``;
- **временная недоступность при старте** (``LLMUnavailable`` — сеть, 5xx каталога OpenRouter;
  БД не отвечает) — повтор в том же процессе с растущей паузой (1 с · 2^n, до 60 с), без
  выхода; пульс (``app.workers.health``) — ``waiting`` с видом причины, контейнер ``Up
  (unhealthy)`` и не перезапускается. БД, недоступная после старта, — повтор захвата в цикле
  воркера (как раньше), пульс не обновляется.

Остановка (SIGTERM) прерывает ожидание при старте: выход с кодом 0."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError

from app.ai.llm import LLMBadOutput, LLMError, LLMUnavailable
from app.config import ConfigError
from app.db import sqlstate
from app.workers.health import Heartbeat

logger = logging.getLogger(__name__)

EX_CONFIG = 78  # sysexits.h: ошибка конфигурации — перезапуск не поможет

# БД отвергла подключение или роль: неверный пароль / способ входа, нет базы, роль входа не
# член feelings_worker (SET ROLE) — лечит оператор, повтор не поможет.
CONFIG_SQLSTATES = frozenset({"28P01", "28000", "3D000", "42501"})
# Ошибки модели при старте, которые проходят сами (сеть, 5xx, испорченный ответ каталога).
TRANSIENT_LLM = (LLMUnavailable, LLMBadOutput)

START_RETRY_FIRST_S = 1.0
START_RETRY_MAX_S = 60.0


def config_db_error(exc: BaseException) -> str | None:
    """SQLSTATE, если ошибка БД — ошибка настройки роли входа (не временная); иначе ``None``."""
    if isinstance(exc, DBAPIError):
        code = sqlstate(exc)
        if code in CONFIG_SQLSTATES:
            return code
    return None


def raise_if_config_db_error(exc: BaseException, *, name: str) -> None:
    """В цикле воркера: БД отвергла роль входа — не «повторить через 30 с», а ошибка настройки."""
    code = config_db_error(exc)
    if code is not None:
        raise ConfigError(
            f"{name}: database rejected the worker login (SQLSTATE {code}): check "
            "FEELINGS_WORKER_DATABASE_URL and db-roles"
        ) from None


def describe_config_error(exc: BaseException) -> str:
    """Текст для журнала без значений: у ``ConfigError`` — свой текст (в нём только имена
    переменных и id ключей); у ошибок проверки настроек — только имена полей; у ошибок модели —
    вид (``auth``, ``model_not_found``…), без ответа поставщика."""
    if isinstance(exc, ValidationError):
        fields = sorted({str(e["loc"][0]).upper() for e in exc.errors() if e.get("loc")})
        return "invalid settings: " + ", ".join(f"FEELINGS_{f}" for f in fields)
    if isinstance(exc, LLMError):
        return f"model provider: {exc.kind}"
    return str(exc)


def is_config_error(exc: BaseException) -> bool:
    if isinstance(exc, ConfigError | ValidationError):
        return True
    return isinstance(exc, LLMError) and not isinstance(exc, TRANSIENT_LLM)


async def startup_retry[T](
    step: Callable[[], Awaitable[T]],
    *,
    name: str,
    what: str,
    heartbeat: Heartbeat,
    stop: asyncio.Event,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T | None:
    """Шаг запуска (модели, каталог цен) с повтором при временной недоступности. Ошибка
    настройки — исключение (``run_worker`` → код 78). ``None`` — пришла остановка."""
    pause = START_RETRY_FIRST_S
    while not stop.is_set():
        try:
            return await step()
        except TRANSIENT_LLM as exc:
            reason = f"{what}_{exc.kind}"
        except Exception as exc:
            from app.workers.pipeline import transient_db_error

            raise_if_config_db_error(exc, name=name)
            if not transient_db_error(exc):
                raise
            reason = f"{what}_unavailable"
        heartbeat.beat("waiting", reason)
        logger.warning("%s: %s at start, retry in %.0fs", name, reason, pause)
        nap = asyncio.ensure_future(sleep(pause))
        waiter = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({nap, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (nap, waiter):
                t.cancel()
        pause = min(pause * 2, START_RETRY_MAX_S)
    return None


def run_worker(main: Callable[[], Coroutine[Any, Any, None]], *, name: str) -> None:
    """Точка входа ``python -m app.workers.<воркер>``: ошибка настройки → код 78."""
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except Exception as exc:
        if not is_config_error(exc):
            raise
        # Без трассировки: в ней могли бы оказаться значения настроек.
        logger.critical(
            "%s: configuration error, exit %d (EX_CONFIG): %s: %s",
            name,
            EX_CONFIG,
            type(exc).__name__,
            describe_config_error(exc),
        )
        sys.exit(EX_CONFIG)

"""Клиент Telegram Bot API на httpx.

Токен бота входит в URL Bot API (`/bot<токен>/<метод>`), поэтому:
- URL собирается на каждый запрос и нигде не хранится; в `repr` клиента токена нет;
- ошибки — только `BotApiError` с методом и кодом в тексте, без URL и исходного исключения
  httpx (`raise ... from None`: в нём есть запрос с URL); описание от Telegram — отдельным полем
  `description` (очищено от токена), его выводит только утилита оператора;
- журнал httpx («HTTP Request: POST <url>») чистит фильтр `app.core.logging`.

Повтор — только на 429 с `retry_after` (не дольше `max_retry_after` секунд, не больше
`max_retries` раз). Тайм-ауты и сетевые сбои не повторяются: повторная отправка могла бы
продублировать сообщение; повтор обеспечивает повторная доставка вебхука. Признак `maybe_sent`
у ошибки различает «запрос мог быть выполнен» (тайм-аут чтения, обрыв) и «точно не выполнен»
(соединение не установлено) — по нему воркер уведомлений (3.5) решает, повторять ли."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import SecretStr

from app.core.logging import redact

API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class BotApiError(Exception):
    """Ошибка вызова Bot API. `transient` — имеет смысл повторить позже (429, 5xx, сеть)."""

    def __init__(
        self,
        method: str,
        reason: str,
        *,
        error_code: int | None = None,
        transient: bool = False,
        retry_after: int | None = None,
        description: str | None = None,
        maybe_sent: bool = False,
    ) -> None:
        super().__init__(f"Bot API {method}: {reason}")
        # Описание от Telegram — отдельным полем, не в str(): его показывает только утилита
        # оператора (app/bot/setup.py), в журналы вебхука оно не идёт.
        self.description = description
        self.method = method
        self.error_code = error_code
        self.transient = transient
        self.retry_after = retry_after
        # Запрос мог дойти до Telegram и быть выполнен (тайм-аут чтения, обрыв после отправки):
        # повтор мог бы продублировать сообщение. ``False`` — точно не выполнен (соединение не
        # установлено) или Telegram ответил ошибкой.
        self.maybe_sent = maybe_sent


class BotApi:
    def __init__(
        self,
        token: str | SecretStr,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        max_retries: int = 2,
        max_retry_after: float = 10.0,
        base_url: str = API_BASE,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._token = token if isinstance(token, SecretStr) else SecretStr(token)
        self._base = base_url.rstrip("/")
        self._max_retries = max_retries
        self._max_retry_after = max_retry_after
        self._sleep = sleep
        self.timeout = timeout
        self._http = httpx.AsyncClient(transport=transport, timeout=timeout)

    def __repr__(self) -> str:
        return f"BotApi(base={self._base!r})"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(
        self, method: str, payload: dict[str, Any], *, http_timeout: httpx.Timeout | None = None
    ) -> Any:
        """`http_timeout` — тайм-ауты httpx одного вызова (долгий опрос getUpdates)."""
        attempt = 0
        while True:
            try:
                return await self._call_once(method, payload, http_timeout)
            except BotApiError as err:
                retry = err.retry_after
                if (
                    err.error_code != 429
                    or retry is None
                    or retry > self._max_retry_after
                    or attempt >= self._max_retries
                ):
                    raise
                attempt += 1
                await self._sleep(retry)

    async def _call_once(
        self, method: str, payload: dict[str, Any], http_timeout: httpx.Timeout | None
    ) -> Any:
        url = f"{self._base}/bot{self._token.get_secret_value()}/{method}"
        try:
            resp = await self._http.post(url, json=payload, timeout=http_timeout or self.timeout)
        except (httpx.ConnectTimeout, httpx.PoolTimeout):
            # Соединение не установлено — запрос до Telegram не дошёл.
            raise BotApiError(method, "timeout", transient=True) from None
        except httpx.TimeoutException:
            raise BotApiError(method, "timeout", transient=True, maybe_sent=True) from None
        except (httpx.ConnectError, httpx.ProxyError, httpx.UnsupportedProtocol):
            raise BotApiError(method, "network error", transient=True) from None
        except httpx.HTTPError:
            raise BotApiError(method, "network error", transient=True, maybe_sent=True) from None
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise BotApiError(
                method,
                f"unexpected response, HTTP {resp.status_code}",
                error_code=resp.status_code,
                transient=resp.status_code >= 500 or resp.status_code == 429,
            ) from None
        if body.get("ok") is True and resp.status_code == 200:
            return body.get("result")
        code = body.get("error_code")
        code = code if isinstance(code, int) else resp.status_code
        params = body.get("parameters")
        retry_after = params.get("retry_after") if isinstance(params, dict) else None
        if not isinstance(retry_after, int):
            retry_after = None
        description = body.get("description")
        # Описание от Telegram — не в тексте ошибки: в нём бывают фрагменты запроса.
        raise BotApiError(
            method,
            f"error_code {code}",
            error_code=code,
            transient=code == 429 or code >= 500,
            retry_after=retry_after,
            description=redact(description)[:300] if isinstance(description, str) else None,
        ) from None

    async def send_message(
        self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None
    ) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self.call("sendMessage", payload)


def webapp_keyboard(text: str, webapp_url: str) -> dict[str, Any]:
    """Встроенная кнопка, открывающая Mini App (работает в личном чате с ботом)."""
    return {"inline_keyboard": [[{"text": text, "web_app": {"url": webapp_url}}]]}


def describe_error(err: BotApiError) -> str:
    """Текст для оператора (утилиты setup/check/poll): метод, код и описание от Telegram
    (без токена). В журналы вебхука и опроса не идёт."""
    return f"{err}: {err.description}" if err.description else str(err)

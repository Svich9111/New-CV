"""Предел размера тела запроса на уровне сервера (задача 2.6): тело больше предела не читается
целиком — ни в память, ни во временный файл разбора формы.

ASGI-обёртка: объявленный ``Content-Length`` больше предела — 413 сразу, приложение не
вызывается; без него (chunked) — байты считаются по мере чтения, на превышении приложение
получает ``http.disconnect`` (чтение тела прекращается), а клиенту уходит 413 вместо ответа
приложения. Ответ — по схеме ``Error``; путь и тело в журнал не пишутся."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping

from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _payload(code: str, message: str) -> bytes:
    return json.dumps({"code": code, "message": message}, ensure_ascii=False).encode()


class BodyLimit:
    def __init__(
        self,
        app: ASGIApp,
        *,
        default: Callable[[], int],
        overrides: Mapping[tuple[str, str], tuple[int, str, str]] | None = None,
    ) -> None:
        """``default`` — предел по умолчанию (читается на каждый запрос: настройка окружения);
        ``overrides``: (метод, путь) → (предел, код, сообщение) — для пути загрузки."""
        self.app = app
        self.default = default
        self.overrides = dict(overrides or {})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit, code, message = self.overrides.get(
            (scope["method"], scope["path"]),
            (self.default(), "payload_too_large", "Запрос слишком большой"),
        )
        declared = _content_length(scope)
        if declared is not None and declared > limit:
            await _reject(send, code, message)
            return
        received = 0
        too_large = False
        started = False

        async def limited_receive() -> Message:
            nonlocal received, too_large
            if too_large:
                return {"type": "http.disconnect"}
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > limit:
                    too_large = True
                    return {"type": "http.disconnect"}
            return msg

        async def guarded_send(msg: Message) -> None:
            nonlocal started
            if too_large:
                if msg["type"] == "http.response.start" and not started:
                    started = True
                    await _reject(send, code, message)
                return  # ответ приложения на оборванное тело не нужен
            started = started or msg["type"] == "http.response.start"
            await send(msg)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not too_large:
                raise
        if too_large and not started:
            await _reject(send, code, message)


def _content_length(scope: Scope) -> int | None:
    for key, value in scope.get("headers", ()):
        if key == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _reject(send: Send, code: str, message: str) -> None:
    body = _payload(code, message)
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})

"""2.6, этап 2 (A2): админ-API базы знаний — `GET/POST /admin/kb/documents`,
`DELETE /admin/kb/documents/{document_id}`; вход администратора (`adminBearer`), предел тела.

ФТ-15; ТЗ §6.4, §9.1 п.5; DECISIONS.md АР-24, АР-39; контракт `KbDocument`, `AdminOnly`.
Подключение администратора в тестах — суперпользователь с `SET LOCAL ROLE feelings_admin`
(`db.admin_scope`): права роли те же, что у роли входа администратора, которой в тестовом
кластере нет (Б-75; роли `feelings_*` общие для кластера и в тестах не создаются). Файлы —
выдуманные, у каждого уникальное содержимое (дубли считаются по SHA-256)."""

from __future__ import annotations

import hashlib
import io
import logging
import secrets
import uuid
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api import admin_kb
from app.config import ConfigError, Settings, check_admin_config, get_settings
from app.db import make_sessionmaker
from app.main import app
from tests.contract import assert_matches
from tests.test_auth_api import SECRET, TOKEN, _init_data, _tg
from tests.test_couple_api import BOT

SM = async_sessionmaker[AsyncSession]
DOCS = "/api/v1/admin/kb/documents"


@dataclass
class Env:
    client: httpx.AsyncClient
    engine: AsyncEngine
    token: str
    other_token: str

    @property
    def admin(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
async def env(sessionmaker: SM, engine: AsyncEngine) -> AsyncIterator[Env]:
    token, other = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

    def settings() -> Settings:
        return Settings(
            telegram_bot_token=SecretStr(TOKEN),
            session_secret=SecretStr(SECRET),
            session_ttl_s=600,
            bot_username=BOT,
            admin_token_sha256=f"{_sha(token)}, {_sha(other).upper()}",  # ротация: два токена
            admin_database_url="postgresql+asyncpg://admin@db.invalid/none",  # не используется
        )

    prev = (app.state.sessionmaker, app.state.admin_sessionmaker)
    app.state.sessionmaker = sessionmaker
    app.state.admin_sessionmaker = make_sessionmaker(engine)
    app.dependency_overrides[get_settings] = settings
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield Env(c, engine, token, other)
    app.dependency_overrides.clear()
    app.state.sessionmaker, app.state.admin_sessionmaker = prev


def book(label: str = "") -> bytes:
    """Выдуманный текст с уникальной меткой (каждый файл — свой SHA-256)."""
    return (
        f"Выдуманная глава о чувствах и потребностях. {label} {uuid.uuid4().hex}\n" * 20
    ).encode()


def docx(label: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f"<w:document>{label}{uuid.uuid4().hex}</w:document>")
    return buf.getvalue()


def epub(label: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")  # первым, без сжатия
        z.writestr("OEBPS/ch1.xhtml", f"<p>{label}{uuid.uuid4().hex}</p>")
    return buf.getvalue()


def pdf(label: str = "") -> bytes:
    return f"%PDF-1.4\n% {label}{uuid.uuid4().hex}\n%%EOF\n".encode()


def form(**over: Any) -> dict[str, str]:
    data = {
        "title": "Выдуманная книга",
        "author": "Автор Выдуманный",
        "approach": "ННО",
        "rights_basis": "Лицензия автора для теста",
    }
    data.update(over)
    return {k: v for k, v in data.items() if v is not None}


async def upload(
    env: Env,
    data: bytes,
    filename: str = "book.txt",
    *,
    headers: dict[str, str] | None = None,
    **fields: Any,
) -> httpx.Response:
    return await env.client.post(
        DOCS,
        headers=env.admin if headers is None else headers,
        data=form(**fields),
        files={"file": (filename, data, "application/octet-stream")},
    )


def _error(resp: httpx.Response, status: int, code: str) -> None:
    assert resp.status_code == status, resp.text
    assert_matches(resp.json(), "Error")
    assert resp.json()["code"] == code, resp.json()


async def _user_headers(c: httpx.AsyncClient) -> dict[str, str]:
    resp = await c.post("/api/v1/auth/telegram", json={"init_data": _init_data(_tg())})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _count_docs(env: Env, sha: str) -> int:
    async with env.engine.connect() as conn:
        n: int = (
            await conn.execute(
                text("SELECT count(*) FROM kb_documents WHERE source_sha256 = decode(:h, 'hex')"),
                {"h": sha},
            )
        ).scalar_one()
    return n


# --------------------------------------------------------------------------- вход администратора


async def test_admin_auth_401_403_and_rotation(env: Env) -> None:
    """Нет заголовка, не bearer, неизвестный токен — 401 `unauthorized`; сессия Mini App — 403
    `admin_only`; оба действующих токена (ротация) — пускают."""
    user = await _user_headers(env.client)
    for headers in (
        {},
        {"Authorization": f"Basic {env.token}"},
        {"Authorization": f"Bearer {secrets.token_urlsafe(32)}"},
        {"Authorization": f"Bearer {_sha(env.token)}"},  # хеш вместо токена — не токен
    ):
        _error(await env.client.get(DOCS, headers=headers), 401, "unauthorized")
        _error(await upload(env, book(), headers=headers), 401, "unauthorized")
        _error(
            await env.client.delete(f"{DOCS}/{uuid.uuid4()}", headers=headers), 401, "unauthorized"
        )
    _error(await env.client.get(DOCS, headers=user), 403, "admin_only")
    _error(await upload(env, book(), headers=user), 403, "admin_only")
    _error(await env.client.delete(f"{DOCS}/{uuid.uuid4()}", headers=user), 403, "admin_only")
    for token in (env.token, env.other_token):
        resp = await env.client.get(DOCS, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
    # Токен администратора — не сессия пользователя: пути Mini App его не принимают.
    _error(await env.client.get("/api/v1/me", headers=env.admin), 401, "unauthorized")


async def test_deleted_user_session_is_401_not_403(env: Env) -> None:
    user = await _user_headers(env.client)
    assert (await env.client.delete("/api/v1/me", headers=user)).status_code == 204
    _error(await env.client.get(DOCS, headers=user), 401, "unauthorized")


async def test_no_admin_tokens_configured_means_closed(env: Env) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(
        session_secret=SecretStr(SECRET), admin_token_sha256=""
    )
    _error(await env.client.get(DOCS, headers=env.admin), 401, "unauthorized")


def test_admin_config_checks() -> None:
    with pytest.raises(ConfigError, match="FEELINGS_ADMIN_DATABASE_URL"):
        check_admin_config(Settings(admin_token_sha256=_sha("x" * 40)))
    with pytest.raises(ConfigError, match="SHA-256") as info:
        check_admin_config(Settings(admin_token_sha256="not-a-hash", admin_database_url="x"))
    assert "not-a-hash" not in str(info.value)
    check_admin_config(Settings())  # без токенов — админ-API закрыт, подключение не нужно
    check_admin_config(Settings(admin_token_sha256=_sha("x" * 40), admin_database_url="x"))


# --------------------------------------------------------------------------- загрузка и список


async def test_upload_txt_then_listed_with_metadata(env: Env) -> None:
    data = book("txt")
    resp = await upload(env, data, title="Выдуманная книга о границах", author="", approach="")
    assert resp.status_code == 202, resp.text
    assert resp.headers["cache-control"] == "no-store"
    doc = resp.json()
    assert_matches(doc, "KbDocument")
    sha = hashlib.sha256(data).hexdigest()
    assert (doc["index_status"], doc["source_format"], doc["source_size"], doc["sha256"]) == (
        "uploaded",
        "txt",
        len(data),
        sha,
    )
    assert doc["author"] is None and doc["approach"] is None  # пустые поля — null
    assert doc["index_error"] is None and doc["indexed_at"] is None
    listed = await env.client.get(DOCS, headers=env.admin)
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
    for item in listed.json():
        assert_matches(item, "KbDocument")
    assert doc in listed.json()
    async with env.engine.connect() as conn:
        audit = (
            await conn.execute(
                text(
                    "SELECT action, actor_id FROM audit_log WHERE object_type = 'kb_document' "
                    "AND object_id = :d"
                ),
                {"d": doc["id"]},
            )
        ).all()
    assert [(a.action, a.actor_id) for a in audit] == [("kb.upload", None)]  # пишет функция БД


@pytest.mark.parametrize(
    ("maker", "filename", "fmt"),
    [(pdf, "a.PDF", "pdf"), (docx, "a.docx", "docx"), (epub, "a.epub", "epub")],
)
async def test_upload_detects_format_by_content_and_extension(
    env: Env, maker: Any, filename: str, fmt: str
) -> None:
    resp = await upload(env, maker(), filename)
    assert resp.status_code == 202, resp.text
    assert resp.json()["source_format"] == fmt


@pytest.mark.parametrize(
    ("data", "filename"),
    [
        (book(), "book.exe"),
        (book(), "book"),
        (book(), "book.pdf"),  # расширение pdf, содержимое — текст
        (pdf(), "book.docx"),
        (docx(), "book.epub"),  # zip без mimetype epub
        (b"PK\x03\x04not a zip", "book.docx"),
        ("\x00бинарный".encode(), "book.txt"),
        (b"\xff\xfe\xfa", "book.txt"),  # не UTF-8
    ],
)
async def test_unsupported_format_415_before_db(env: Env, data: bytes, filename: str) -> None:
    resp = await upload(env, data, filename)
    _error(resp, 415, "unsupported_format")
    assert await _count_docs(env, hashlib.sha256(data).hexdigest()) == 0


async def test_duplicate_is_409(env: Env) -> None:
    data = book("dup")
    first = await upload(env, data)
    assert first.status_code == 202
    _error(await upload(env, data, title="Другое название"), 409, "duplicate_document")
    assert await _count_docs(env, hashlib.sha256(data).hexdigest()) == 1


async def test_file_over_20_mib_is_413_before_db(env: Env) -> None:
    data = b"a" * (admin_kb.MAX_FILE_BYTES + 1)
    _error(await upload(env, data), 413, "file_too_large")
    assert await _count_docs(env, hashlib.sha256(data).hexdigest()) == 0
    exact = (b"b" * (admin_kb.MAX_FILE_BYTES - 33)) + uuid.uuid4().hex.encode() + b"\n"
    assert len(exact) == admin_kb.MAX_FILE_BYTES
    assert (await upload(env, exact)).status_code == 202  # ровно 20 МиБ — принимается


async def test_request_body_limited_at_server_level(env: Env) -> None:
    """Тело больше предела не читается: объявленная длина — 413 сразу; без длины (chunked) —
    чтение обрывается на пределе; на прочих путях — общий предел (1 МиБ по умолчанию)."""
    big = admin_kb.MAX_UPLOAD_BODY_BYTES + 1
    resp = await env.client.post(
        DOCS,
        headers={**env.admin, "Content-Type": "multipart/form-data; boundary=x"},
        content=b"x" * big,
    )
    _error(resp, 413, "file_too_large")
    sent = 0

    async def chunks() -> AsyncIterator[bytes]:
        nonlocal sent
        yield (
            b'--x\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n'
            b"Content-Type: text/plain\r\n\r\n"
        )
        for _ in range(64):
            sent += 1024 * 1024
            yield b"y" * (1024 * 1024)

    resp = await env.client.post(
        DOCS,
        headers={**env.admin, "Content-Type": "multipart/form-data; boundary=x"},
        content=chunks(),
    )
    assert "content-length" not in {k.lower() for k in resp.request.headers}
    _error(resp, 413, "file_too_large")
    assert sent < 40 * 1024 * 1024  # чтение оборвано на пределе, не все 64 МиБ
    user = await _user_headers(env.client)
    resp = await env.client.post(
        "/api/v1/chat/messages",
        headers={**user, "Content-Type": "application/json"},
        content=b'{"text": "' + b"z" * (2 * 1024 * 1024) + b'"}',
    )
    _error(resp, 413, "payload_too_large")


@pytest.mark.parametrize(
    "fields",
    [
        {"title": None},
        {"title": ""},
        {"title": "   "},
        {"title": "т" * 301},
        {"rights_basis": None},
        {"rights_basis": "  "},
        {"author": "а" * 201},
        {"approach": "п" * 101},
        {"extra": "поле"},
    ],
)
async def test_invalid_fields_422(env: Env, fields: dict[str, Any]) -> None:
    data = book("422")
    _error(await upload(env, data, **fields), 422, "validation_error")
    assert await _count_docs(env, hashlib.sha256(data).hexdigest()) == 0


async def test_missing_or_empty_file_422(env: Env) -> None:
    resp = await env.client.post(DOCS, headers=env.admin, data=form())
    _error(resp, 422, "validation_error")
    _error(await upload(env, b""), 422, "validation_error")


# --------------------------------------------------------------------------- удаление


async def test_delete_204_then_404(env: Env) -> None:
    doc = (await upload(env, book("del"))).json()
    resp = await env.client.delete(f"{DOCS}/{doc['id']}", headers=env.admin)
    assert resp.status_code == 204 and resp.content == b""
    assert doc["id"] not in {
        d["id"] for d in (await env.client.get(DOCS, headers=env.admin)).json()
    }
    for target in (doc["id"], str(uuid.uuid4()), "not-a-uuid"):
        _error(await env.client.delete(f"{DOCS}/{target}", headers=env.admin), 404, "not_found")
    async with env.engine.connect() as conn:
        actions = (
            await conn.execute(
                text("SELECT action FROM audit_log WHERE object_id = :d ORDER BY created_at"),
                {"d": doc["id"]},
            )
        ).scalars()
        assert list(actions) == ["kb.upload", "kb.delete"]


# --------------------------------------------------------------------------- журналы


async def test_logs_have_no_tokens_titles_or_content(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    label = f"метка-{uuid.uuid4().hex}"
    data = book(label)
    doc = (await upload(env, data, title=f"Название {label}")).json()
    _error(await upload(env, data, title=f"Название {label}"), 409, "duplicate_document")
    await env.client.get(DOCS, headers=env.admin)
    await env.client.delete(f"{DOCS}/{doc['id']}", headers=env.admin)
    _error(
        await env.client.get(DOCS, headers={"Authorization": "Bearer wrong-token-value"}),
        401,
        "unauthorized",
    )
    logged = "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)
    for secret in (env.token, _sha(env.token), env.other_token, label, "wrong-token-value"):
        assert secret not in logged
    assert "admin kb upload: format=txt" in logged
    async with env.engine.connect() as conn:
        dump = (
            await conn.execute(
                text("SELECT coalesce(string_agg(t::text, ' '), '') FROM audit_log t")
            )
        ).scalar_one()
    assert label not in dump

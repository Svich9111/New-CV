"""Админ-API базы знаний — `GET/POST /admin/kb/documents`, `DELETE /admin/kb/documents/{id}`
(ФТ-15; ТЗ §6.4, §9.1 п.5; задача 2.6, этап 2; АР-39).

**Вход администратора** (`adminBearer`): bearer-токен, не Telegram. Сервер хранит только SHA-256
действующих токенов (`FEELINGS_ADMIN_TOKEN_SHA256`, через запятую — ротация) и сравнивает
SHA-256 предъявленного со всеми за постоянное время (`hmac.compare_digest`, без раннего выхода).
Нет заголовка или токен неизвестен — 401 `unauthorized`; предъявлена действующая сессия
пользователя Mini App — 403 `admin_only`. Запросы — под ролью `feelings_admin`
(`db.admin_scope`) через отдельное подключение администратора (`app.state.admin_sessionmaker`,
роль входа — Б-75), не через подключение API. Токены и их хеши в журнал не пишутся.

**Загрузка**: сначала вход (тело до проверки не разбирается), затем форма multipart: поля —
по контракту (422), файл читается кусками с пределом 20 МиБ (413 `file_too_large` до БД; само
тело запроса ограничено на уровне сервера — `core.body_limit`), формат — по содержимому и
расширению (415 `unsupported_format`), затем `admin_kb_upload`: FK001 → 413, FK002 → 409
`duplicate_document`, 22023 → 422. Журнал действий пишут функции БД (АР-24), обработчики — нет.
Ответы не кэшируются (`Cache-Control: no-store`)."""

from __future__ import annotations

import hashlib
import hmac
import io
import logging
import uuid
import zipfile
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import UploadFile

from app.api.deps import AppSettings, get_sessionmaker, unauthorized
from app.config import Settings, admin_token_hashes
from app.core.session import SessionError, read_token
from app.errors import AppError, not_found
from app.kb.parse import ParseError, ParseLimits, parse_txt
from app.repositories import kb_admin, users
from app.repositories.kb_admin import Duplicate, InvalidUpload, KbDocumentRow, TooLarge

router = APIRouter(tags=["admin"])
logger = logging.getLogger(__name__)

UPLOAD_PATH = "/api/v1/admin/kb/documents"
MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 МиБ — как CHECK `source_data` (АР-39)
# Предел тела запроса загрузки на уровне сервера: файл + поля формы и разметка multipart.
MAX_UPLOAD_BODY_BYTES = MAX_FILE_BYTES + 1024 * 1024
READ_CHUNK = 1024 * 1024
MAX_FORM_FIELDS = 10
MAX_FIELD_BYTES = 64 * 1024

_bearer = HTTPBearer(auto_error=False)


def file_too_large() -> AppError:
    return AppError(413, "file_too_large", "Файл больше 20 МиБ")


def unsupported_format() -> AppError:
    return AppError(415, "unsupported_format", "Поддерживаются PDF, EPUB, DOCX и TXT")


def _validation() -> AppError:
    return AppError(422, "validation_error", "Проверьте данные")


# --------------------------------------------------------------------------- вход администратора


def is_admin_token(token: str, hashes: tuple[bytes, ...]) -> bool:
    """SHA-256 токена совпал с одним из действующих; сравнение — со всеми, за постоянное время."""
    digest = hashlib.sha256(token.encode()).digest()
    found = False
    for known in hashes:
        found |= hmac.compare_digest(digest, known)
    return found


async def admin_access(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: AppSettings,
) -> None:
    if creds is None or creds.scheme.lower() != "bearer":
        raise unauthorized()
    if is_admin_token(creds.credentials, admin_token_hashes(settings)):
        return
    if await _is_user_session(request, creds.credentials, settings):
        raise AppError(403, "admin_only", "Доступно только администратору")
    raise unauthorized()


async def _is_user_session(request: Request, token: str, settings: Settings) -> bool:
    """Действующая сессия Mini App (подпись верна, пользователь не удалён) — 403; удалённый
    пользователь — 401, как на любом пути (ФТ-21)."""
    secret = settings.session_secret.get_secret_value()
    if not secret:
        return False
    try:
        user_id = read_token(token, secret)
    except SessionError:
        return False
    return await users.is_active(get_sessionmaker(request), user_id)


AdminAccess = Annotated[None, Depends(admin_access)]


def admin_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    sm: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "admin_sessionmaker", None
    )
    if sm is None:  # токены без подключения не пропускает check_admin_config
        raise RuntimeError("admin database is not configured")
    return sm


AdminSM = Annotated[async_sessionmaker[AsyncSession], Depends(admin_sessionmaker)]


# --------------------------------------------------------------------------- схемы


class KbDocument(BaseModel):
    id: uuid.UUID
    title: str
    author: str | None
    approach: str | None
    rights_basis: str
    index_status: Literal["uploaded", "indexing", "indexed", "failed"]
    index_error: str | None
    source_format: Literal["pdf", "epub", "docx", "txt"] | None
    source_size: int | None
    sha256: str | None
    created_at: datetime
    indexed_at: datetime | None


def _out(row: KbDocumentRow) -> KbDocument:
    return KbDocument.model_validate(row, from_attributes=True)


class UploadFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str = Field(min_length=1, max_length=300)
    author: str | None = Field(default=None, max_length=200)
    approach: str | None = Field(default=None, max_length=100)
    rights_basis: str = Field(min_length=1)

    @field_validator("title", "rights_basis")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("пустое поле")
        return value

    @field_validator("author", "approach")
    @classmethod
    def _empty_is_none(cls, value: str | None) -> str | None:
        return value if value is not None and value.strip() else None


# --------------------------------------------------------------------------- формат


_TXT_LIMITS = ParseLimits()
_ZIP = b"PK\x03\x04"
_EPUB_MIMETYPE = b"mimetypeapplication/epub+zip"


def detect_format(filename: str, data: bytes) -> str | None:
    """Формат по расширению **и** содержимому (одно расширение не доверяется)."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "pdf" and data.startswith(b"%PDF-"):
        return "pdf"
    if ext == "epub" and data.startswith(_ZIP) and data[30:58] == _EPUB_MIMETYPE:
        return "epub"
    if ext == "docx" and data.startswith(_ZIP) and _zip_has(data, "word/document.xml"):
        return "docx"
    if ext == "txt" and _is_text(data):
        return "txt"
    return None


def _zip_has(data: bytes, name: str) -> bool:
    """Только оглавление архива (содержимое не распаковывается — разбор и защита от бомб — у
    воркера индексации)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return name in archive.namelist()
    except (zipfile.BadZipFile, ValueError, OSError):
        return False


def _is_text(data: bytes) -> bool:
    """TXT в UTF-8 или CP1251 — та же проверка, что у разборщика (``kb.parse.parse_txt``:
    кодировки и отсечение двоичного мусора по доле управляющих символов), плюс без NUL.
    Достаточно ли в нём букв (``empty_text``), решает индексация."""
    if b"\x00" in data:
        return False
    try:
        parse_txt(data, _TXT_LIMITS)
    except ParseError:
        return False
    return True


async def _read_limited(upload: UploadFile) -> bytes:
    buf = bytearray()
    while chunk := await upload.read(READ_CHUNK):
        buf += chunk
        if len(buf) > MAX_FILE_BYTES:
            raise file_too_large()
    return bytes(buf)


# --------------------------------------------------------------------------- пути


@router.get("/admin/kb/documents")
async def list_documents(_: AdminAccess, sm: AdminSM, response: Response) -> list[KbDocument]:
    rows = await kb_admin.list_documents(sm)
    response.headers["Cache-Control"] = "no-store"
    return [_out(r) for r in rows]


@router.post("/admin/kb/documents", status_code=202)
async def upload_document(
    _: AdminAccess, request: Request, sm: AdminSM, response: Response
) -> KbDocument:
    form = await request.form(
        max_files=1, max_fields=MAX_FORM_FIELDS, max_part_size=MAX_FIELD_BYTES
    )
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise _validation()
        raw: dict[str, Any] = {}
        for key, value in form.multi_items():
            if key == "file":
                continue
            if not isinstance(value, str) or key in raw:
                raise _validation()
            raw[key] = value
        try:
            fields = UploadFields.model_validate(raw)
        except ValidationError as exc:
            raise _validation() from exc
        data = await _read_limited(upload)
        if not data:
            raise _validation()
        fmt = detect_format(upload.filename or "", data)
        if fmt is None:
            raise unsupported_format()
    finally:
        await form.close()
    try:
        row = await kb_admin.upload(
            sm,
            title=fields.title,
            author=fields.author,
            approach=fields.approach,
            rights_basis=fields.rights_basis,
            source_format=fmt,
            data=data,
        )
    except TooLarge as exc:
        raise file_too_large() from exc
    except Duplicate as exc:
        raise AppError(409, "duplicate_document", "Этот файл уже загружен") from exc
    except InvalidUpload as exc:
        raise _validation() from exc
    logger.info("admin kb upload: format=%s size=%s", fmt, len(data))
    response.headers["Cache-Control"] = "no-store"
    return _out(row)


@router.delete("/admin/kb/documents/{document_id}", status_code=204)
async def delete_document(_: AdminAccess, sm: AdminSM, document_id: str) -> Response:
    try:
        doc = uuid.UUID(document_id)
    except ValueError as exc:
        raise not_found() from exc
    if not await kb_admin.delete(sm, doc):
        raise not_found()
    logger.info("admin kb delete: done")
    return Response(status_code=204, headers={"Cache-Control": "no-store"})

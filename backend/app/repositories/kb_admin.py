"""Слой доступа администратора базы знаний (ФТ-15; задача 2.6, этап 2; АР-39).

Всё — в ``admin_scope`` (роль ``feelings_admin``): загрузка и удаление — только функциями БД
``admin_kb_upload`` / ``admin_kb_delete`` (они же пишут ``audit_log``, АР-24); чтение — колонки
метаданных ``kb_documents`` (исходник и аренда роли не видны). Личных данных роль не видит."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import admin_scope, sqlstate

_COLUMNS = (
    "id, title, author, approach, rights_basis, index_status::text AS index_status, "
    "index_error, source_format::text AS source_format, source_size, source_sha256, "
    "created_at, indexed_at"
)
_LIST = f"SELECT {_COLUMNS} FROM kb_documents ORDER BY created_at DESC, id DESC"  # noqa: S608 — константа
_ONE = f"SELECT {_COLUMNS} FROM kb_documents WHERE id = :i"  # noqa: S608 — константа


class TooLarge(Exception):
    """FK001: файл больше 20 МиБ."""


class Duplicate(Exception):
    """FK002: этот файл уже загружен (не в ``failed``)."""


class InvalidUpload(Exception):
    """22023: пустое/длинное поле, неизвестный формат, пустой файл."""


@dataclass(frozen=True)
class KbDocumentRow:
    id: uuid.UUID
    title: str
    author: str | None
    approach: str | None
    rights_basis: str
    index_status: str
    index_error: str | None
    source_format: str | None
    source_size: int | None
    sha256: str | None  # hex
    created_at: datetime
    indexed_at: datetime | None


def _row(r: object) -> KbDocumentRow:
    m = r._mapping  # type: ignore[attr-defined]
    sha = m["source_sha256"]
    return KbDocumentRow(
        id=m["id"] if isinstance(m["id"], uuid.UUID) else uuid.UUID(str(m["id"])),
        title=m["title"],
        author=m["author"],
        approach=m["approach"],
        rights_basis=m["rights_basis"],
        index_status=m["index_status"],
        index_error=m["index_error"],
        source_format=m["source_format"],
        source_size=m["source_size"],
        sha256=bytes(sha).hex() if sha is not None else None,
        created_at=m["created_at"],
        indexed_at=m["indexed_at"],
    )


async def list_documents(sm: async_sessionmaker[AsyncSession]) -> list[KbDocumentRow]:
    async with admin_scope(sm) as s:
        return [_row(r) for r in (await s.execute(text(_LIST))).all()]


async def upload(
    sm: async_sessionmaker[AsyncSession],
    *,
    title: str,
    author: str | None,
    approach: str | None,
    rights_basis: str,
    source_format: str,
    data: bytes,
) -> KbDocumentRow:
    """``admin_kb_upload`` и метаданные нового материала — одной транзакцией."""
    try:
        async with admin_scope(sm) as s:
            doc = (
                await s.execute(
                    text("SELECT admin_kb_upload(:t, :a, :ap, :r, :f, :s)"),
                    {
                        "t": title,
                        "a": author,
                        "ap": approach,
                        "r": rights_basis,
                        "f": source_format,
                        "s": data,
                    },
                )
            ).scalar_one()
            return _row((await s.execute(text(_ONE), {"i": doc})).one())
    except DBAPIError as exc:
        code = sqlstate(exc)
        if code == "FK001":
            raise TooLarge from exc
        if code == "FK002":
            raise Duplicate from exc
        if code == "22023":
            raise InvalidUpload from exc
        raise


async def delete(sm: async_sessionmaker[AsyncSession], document_id: uuid.UUID) -> bool:
    async with admin_scope(sm) as s:
        return bool(
            (await s.execute(text("SELECT admin_kb_delete(:d)"), {"d": document_id})).scalar_one()
        )

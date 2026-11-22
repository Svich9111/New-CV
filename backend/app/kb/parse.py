"""Извлечение текста из PDF/EPUB/DOCX/TXT (ТЗ §6.4, ФТ-15; задача 2.6).

Только извлечение текста — ничего не исполняется: PDF — ``pypdf`` (чистый Python, без
JavaScript, без внешних программ), EPUB и DOCX — ``zipfile`` стандартной библиотеки + XML через
``defusedxml`` (запрет DTD и сущностей: «миллиард смехов», внешние сущности) + ``html.parser``
для XHTML, TXT — декодирование. Защиты (``ParseLimits``):

- размер файла (как в БД, 20 МиБ) — ``too_large``;
- PDF: зашифрованный — ``encrypted``, страниц больше предела — ``too_many_pages``,
  не читается — ``corrupt``;
- EPUB/DOCX: число файлов в архиве, распакованный размер (по заголовкам и по факту чтения —
  заголовки могут врать), степень сжатия одного файла — ``zip_bomb``; не архив или нет нужных
  частей — ``corrupt``;
- текста меньше ``min_letters`` букв — ``empty_text``; текста больше ``max_text_chars`` —
  ``too_large``;
- тайм-аут и память: ``parse_isolated`` разбирает в отдельном процессе (``spawn``) с пределом
  памяти (``RLIMIT_AS``) и убивает его по тайм-ауту — ``timeout``; процесс упал — ``corrupt``.

Коды причин — ``^[a-z][a-z0-9_]{0,31}$`` (``kb_fail_index``), без текста книги."""

from __future__ import annotations

import io
import multiprocessing
import posixpath
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Literal

Format = Literal["pdf", "epub", "docx", "txt"]
FORMATS: tuple[Format, ...] = ("pdf", "epub", "docx", "txt")

TOO_LARGE = "too_large"
TOO_MANY_PAGES = "too_many_pages"
ZIP_BOMB = "zip_bomb"
ENCRYPTED = "encrypted"
CORRUPT = "corrupt"
EMPTY_TEXT = "empty_text"
TIMEOUT = "timeout"
UNSUPPORTED = "unsupported_format"


class ParseError(Exception):
    """Файл не разобран; ``code`` — причина для ``kb_fail_index`` (без текста)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ParseLimits:
    max_bytes: int = 20 * 1024 * 1024  # как CHECK 0011
    max_pages: int = 2000
    max_zip_entries: int = 2000
    max_unzipped_bytes: int = 200 * 1024 * 1024
    max_ratio: int = 200  # распакованный / сжатый размер одного файла
    max_text_chars: int = 5_000_000
    min_letters: int = 50
    memory_bytes: int = 1024 * 1024 * 1024  # предел памяти процесса разбора
    timeout_s: float = 60.0


# --------------------------------------------------------------------------- общие


def _check_text(text: str, limits: ParseLimits) -> str:
    if len(text) > limits.max_text_chars:
        raise ParseError(TOO_LARGE)
    if sum(ch.isalpha() for ch in text) < limits.min_letters:
        raise ParseError(EMPTY_TEXT)
    return text


# --------------------------------------------------------------------------- TXT


def parse_txt(data: bytes, limits: ParseLimits) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        controls = sum(1 for ch in text if ord(ch) < 32 and ch not in "\n\r\t\f")
        if controls > max(len(text) // 100, 5):
            raise ParseError(CORRUPT)  # двоичные данные, а не текст
        return text
    raise ParseError(CORRUPT)


# --------------------------------------------------------------------------- PDF


def parse_pdf(data: bytes, limits: ParseLimits) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ParseError(ENCRYPTED)
        pages = reader.pages
        if len(pages) > limits.max_pages:
            raise ParseError(TOO_MANY_PAGES)
        parts: list[str] = []
        total = 0
        for page in pages:
            piece = page.extract_text() or ""
            total += len(piece)
            if total > limits.max_text_chars:
                raise ParseError(TOO_LARGE)
            parts.append(piece)
    except ParseError:
        raise
    except MemoryError:
        raise ParseError(TOO_LARGE) from None
    except Exception:  # любые сбои разбора — повреждённый файл (не падение воркера)
        raise ParseError(CORRUPT) from None
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- ZIP (EPUB, DOCX)


class _Zip:
    """Архив под защитой от «бомб»: число файлов, размер по заголовкам и фактическое чтение."""

    def __init__(self, data: bytes, limits: ParseLimits) -> None:
        self.limits = limits
        self.read_total = 0
        try:
            self.zf = zipfile.ZipFile(io.BytesIO(data))
            infos = self.zf.infolist()
        except (zipfile.BadZipFile, OSError, ValueError, EOFError):
            raise ParseError(CORRUPT) from None
        if len(infos) > limits.max_zip_entries:
            raise ParseError(ZIP_BOMB)
        if sum(i.file_size for i in infos) > limits.max_unzipped_bytes:
            raise ParseError(ZIP_BOMB)
        for i in infos:
            if i.flag_bits & 0x1:
                raise ParseError(ENCRYPTED)
            if i.compress_size and i.file_size / i.compress_size > limits.max_ratio:
                raise ParseError(ZIP_BOMB)
        self.names = {i.filename for i in infos}

    def read(self, name: str) -> bytes:
        if name not in self.names:
            raise ParseError(CORRUPT)
        out = bytearray()
        try:
            with self.zf.open(name) as fh:
                while chunk := fh.read(64 * 1024):
                    out += chunk
                    self.read_total += len(chunk)
                    if self.read_total > self.limits.max_unzipped_bytes:
                        raise ParseError(ZIP_BOMB)  # заголовки соврали о размере
        except ParseError:
            raise
        except (zipfile.BadZipFile, OSError, ValueError, EOFError, RuntimeError):
            raise ParseError(CORRUPT) from None
        return bytes(out)


def _xml(data: bytes) -> Any:
    from defusedxml import ElementTree
    from defusedxml.common import DefusedXmlException

    try:
        return ElementTree.fromstring(data)
    except DefusedXmlException:
        raise ParseError(CORRUPT) from None  # DTD/сущности — не разбираем
    except Exception:
        raise ParseError(CORRUPT) from None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_docx(data: bytes, limits: ParseLimits) -> str:
    z = _Zip(data, limits)
    root = _xml(z.read("word/document.xml"))
    paragraphs: list[str] = []
    for p in root.iter():
        if _local(p.tag) != "p":
            continue
        parts: list[str] = []
        for node in p.iter():
            name = _local(node.tag)
            if name == "t" and node.text:
                parts.append(node.text)
            elif name == "tab":
                parts.append(" ")
            elif name in ("br", "cr"):
                parts.append("\n")
        line = "".join(parts).strip()
        if line:
            paragraphs.append(line)
    return "\n\n".join(paragraphs)


_BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "tr", "section"}


class _Text(HTMLParser):
    """Текст XHTML без скриптов и стилей; блоки — абзацы."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head"):
            self.skip = max(self.skip - 1, 0)
        elif tag in _BLOCK:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def parse_epub(data: bytes, limits: ParseLimits) -> str:
    z = _Zip(data, limits)
    container = _xml(z.read("META-INF/container.xml"))
    rootfile = next(
        (e.get("full-path") for e in container.iter() if _local(e.tag) == "rootfile"), None
    )
    if not rootfile:
        raise ParseError(CORRUPT)
    opf = _xml(z.read(rootfile))
    base = posixpath.dirname(rootfile)
    manifest = {
        e.get("id"): e.get("href")
        for e in opf.iter()
        if _local(e.tag) == "item" and e.get("id") and e.get("href")
    }
    spine = [e.get("idref") for e in opf.iter() if _local(e.tag) == "itemref"]
    texts: list[str] = []
    for idref in spine:
        href = manifest.get(idref)
        if not href:
            continue
        name = posixpath.normpath(posixpath.join(base, href.split("#", 1)[0]))
        raw = z.read(name)
        parser = _Text()
        try:
            parser.feed(raw.decode("utf-8", errors="replace"))
            parser.close()
        except Exception:
            raise ParseError(CORRUPT) from None
        texts.append("".join(parser.parts))
    return "\n\n".join(texts)


# --------------------------------------------------------------------------- вход


_PARSERS = {"pdf": parse_pdf, "epub": parse_epub, "docx": parse_docx, "txt": parse_txt}


def parse(data: bytes, fmt: str, limits: ParseLimits | None = None) -> str:
    """Текст файла (абзацы — через пустую строку) или ``ParseError(code)``. В этом процессе —
    без тайм-аута; для недоверенных файлов — ``parse_isolated``."""
    limits = limits or ParseLimits()
    if len(data) > limits.max_bytes:
        raise ParseError(TOO_LARGE)
    if not data:
        raise ParseError(EMPTY_TEXT)
    parser = _PARSERS.get(fmt)
    if parser is None:
        raise ParseError(UNSUPPORTED)
    try:
        text = parser(data, limits)
    except RecursionError:
        raise ParseError(CORRUPT) from None
    return _check_text(re.sub(r"\r\n?", "\n", text), limits)


def _child(conn: Any, data: bytes, fmt: str, limits: ParseLimits) -> None:  # pragma: no cover
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
    except (ImportError, ValueError, OSError):
        pass
    try:
        conn.send(("ok", parse(data, fmt, limits)))
    except ParseError as exc:
        conn.send(("error", exc.code))
    except MemoryError:
        conn.send(("error", TOO_LARGE))
    except Exception:
        conn.send(("error", CORRUPT))
    finally:
        conn.close()


def parse_isolated(data: bytes, fmt: str, limits: ParseLimits | None = None) -> str:
    """Разбор в отдельном процессе: тайм-аут (``timeout``), предел памяти, падение разборщика не
    роняет воркер. Блокирующий вызов — из асинхронного кода через ``asyncio.to_thread``."""
    limits = limits or ParseLimits()
    if len(data) > limits.max_bytes:
        raise ParseError(TOO_LARGE)
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(child, data, fmt, limits), daemon=True)
    proc.start()
    child.close()
    try:
        if not parent.poll(limits.timeout_s):
            raise ParseError(TIMEOUT)
        try:
            kind, value = parent.recv()
        except (EOFError, OSError):
            raise ParseError(CORRUPT) from None
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(5)
        parent.close()
    if kind == "error":
        raise ParseError(str(value))
    return str(value)

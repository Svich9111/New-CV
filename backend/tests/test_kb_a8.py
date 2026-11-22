"""2.6, A8: независимая проверка базы знаний (ФТ-15; ТЗ §6.1 п.4, §6.4, §9.1, §10; АР-39; Б-67) —
разбор (`app.kb.parse`), разбиение (`app.kb.chunk`), метки (`app.kb.tag`), воркер индексации
(`app.workers.kb`), поиск (`DbKnowledgeBase`), запрет цитирования (`app.ai.quotes`, поток 2.1 и
`dialog.reply`), админ-API (`/admin/kb/documents`).

Только выдуманные тексты; файлы генерируются в тесте. Модели и эмбеддер — заглушки (сеть не
используется). Хелперы исполнителей (`test_kb_index.py`, `test_admin_kb_api.py`) не используются.
Дефекты — `xfail(strict=True)` (docs/reviews/2.6-test.md)."""

from __future__ import annotations

import asyncio
import hashlib
import io
import itertools
import json
import logging
import random
import re
import struct
import uuid
import zipfile
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.ai import dialog
from app.ai.chat import Delta, Done, stream_reply
from app.ai.dialog import OwnerContext, Turn
from app.ai.embeddings import HashingEmbedder
from app.ai.knowledge import KNOWLEDGE_LIMIT, KNOWLEDGE_MIN, DbKnowledgeBase
from app.ai.llm import ChatMessage, LLMUnavailable, StructuredResult, Usage
from app.ai.models import AgentModels
from app.ai.quotes import QUOTE_WORDS, SAFE_TAIL, QuoteGuard, QuoteIndex, find_quote
from app.config import Settings, get_settings
from app.core.fingerprint import normalize
from app.db import make_sessionmaker
from app.kb.chunk import ChunkParams, normalize_text, split_chunks
from app.kb.parse import ParseError, ParseLimits, parse, parse_isolated
from app.kb.tag import TOPICS, tag_chunks
from app.main import app
from app.repositories import kb_work
from app.workers.kb import KbSettings, KbWorker
from tests.test_chat_a8 import FakeModel, _answer, _chunks, models_of

SM = async_sessionmaker[AsyncSession]
CODE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_rng = random.Random(2026)  # noqa: S311 — не криптография

WORDS = (
    "мост тишина ладонь окно дорога утро вечер слово чашка память сад река голос шаг свет "
    "тень ветер берег камень ключ поле лес гора облако письмо песня дом дверь стол"
).split()


def sentence(rng: random.Random = _rng, n: int | None = None) -> str:
    n = n or rng.randint(5, 18)
    words = [rng.choice(WORDS) for _ in range(n)]
    return words[0].capitalize() + " " + " ".join(words[1:]) + rng.choice(".!?")


def book(paragraphs: int = 12, rng: random.Random = _rng) -> str:
    marker = f"Метка{uuid.uuid4().hex[:8]}"
    paras = [" ".join(sentence(rng) for _ in range(rng.randint(2, 9))) for _ in range(paragraphs)]
    paras[0] = f"{marker} начинает книгу. " + paras[0]
    return "\n\n".join(paras)


# =============================================================== генераторы файлов


def make_docx(paragraphs: Sequence[str], *, document_xml: str | None = None) -> bytes:
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' for p in paragraphs)
    xml = document_xml or (
        '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats'
        f'.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def make_epub(chapters: Sequence[str], *, opf: str | None = None, container: str | None = None,
              extra: Mapping[str, bytes] | None = None) -> bytes:  # fmt: skip
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")  # без сжатия, первым
        z.writestr(
            "META-INF/container.xml",
            container
            or '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:'
            'container"><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles>'
            "</container>",
        )
        items = "".join(f'<item id="c{i}" href="c{i}.xhtml"/>' for i in range(len(chapters)))
        spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
        z.writestr(
            "OEBPS/content.opf",
            opf
            or f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf"><manifest>'
            f"{items}</manifest><spine>{spine}</spine></package>",
        )
        for i, ch in enumerate(chapters):
            z.writestr(
                f"OEBPS/c{i}.xhtml",
                ch
                if ch.startswith("<?xml")
                else f"<html><head><title>T</title><style>p{{}}</style></head><body>"
                f"<script>alert('СКРИПТa8')</script><p>{ch}</p></body></html>",
            )
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def make_pdf(lines: Sequence[str]) -> bytes:
    content = "BT /F1 12 Tf 72 760 Td " + " ".join(f"({ln}) Tj 0 -14 Td" for ln in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


LATIN = [f"Line {i} of the invented guide about calm talks and gentle requests." for i in range(8)]


def _bomb_zip(size: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", b"\x00" * size)
    return buf.getvalue()


def _lie_about_size(data: bytes, name: bytes, fake: int) -> bytes:
    """Заголовки архива врут о распакованном размере файла ``name`` (локальный и центральный)."""
    b = bytearray(data)
    for sig, off in ((b"PK\x03\x04", 22), (b"PK\x01\x02", 24)):
        i = 0
        while (i := b.find(sig, i)) >= 0:
            n_off = 30 if sig == b"PK\x03\x04" else 46
            ln = struct.unpack_from("<H", b, i + (26 if sig == b"PK\x03\x04" else 28))[0]
            if bytes(b[i + n_off : i + n_off + ln]) == name:
                struct.pack_into("<I", b, i + off, fake)
            i += 4
    return bytes(b)


def _encrypted_flag(data: bytes) -> bytes:
    b = bytearray(data)
    for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        i = 0
        while (i := b.find(sig, i)) >= 0:
            flags = struct.unpack_from("<H", b, i + off)[0]
            struct.pack_into("<H", b, i + off, flags | 0x1)
            i += 4
    return bytes(b)


LAUGHS = (
    '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
    + "".join(
        f'<!ENTITY lol{i} "{("&lol" + (str(i - 1) if i > 1 else "") + ";") * 10}">'
        for i in range(1, 10)
    )
    + "]>"
)
XXE = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'


# =============================================================== 1. Разбор


def test_parse_four_formats() -> None:
    """Кр.3, §6.4: TXT (UTF-8 c BOM, CP1251), DOCX, EPUB (скрипты/стили не попадают), PDF —
    текст извлечён, абзацы сохранены."""
    txt = book(4)
    assert parse(txt.encode("utf-8-sig"), "txt").strip().startswith(txt.split()[0])
    assert "начинает книгу" in parse(txt.encode("cp1251"), "txt")
    paras = txt.split("\n\n")
    out = parse(make_docx(paras), "docx")
    assert [p.strip() for p in out.split("\n\n") if p.strip()] == paras
    epub = parse(make_epub(paras[:2]), "epub")
    assert paras[0] in epub and paras[1] in epub and "СКРИПТa8" not in epub and "p{}" not in epub
    pdf = parse(make_pdf(LATIN), "pdf")
    assert "invented guide" in pdf


BAD_FILES: dict[str, tuple[bytes, str, ParseLimits, set[str]]] = {
    "zip_bomb_ratio": (_bomb_zip(3_000_000), "docx", ParseLimits(), {"zip_bomb"}),
    "zip_bomb_headers_total": (
        _lie_about_size(make_docx(["абв " * 30]), b"word/document.xml", 300 * 1024 * 1024),
        "docx", ParseLimits(), {"zip_bomb"},
    ),
    "zip_lies_small": (
        _lie_about_size(_bomb_zip(300_000), b"word/document.xml", 2_000),
        "docx", ParseLimits(max_ratio=10_000), {"corrupt", "zip_bomb"},
    ),
    "zip_many_files": (
        make_epub(["текст " * 20], extra={f"x/{i}.txt": b"x" for i in range(60)}),
        "epub", ParseLimits(max_zip_entries=50), {"zip_bomb"},
    ),
    "zip_encrypted_flag": (_encrypted_flag(make_docx(["абв " * 30])), "docx", ParseLimits(),
                           {"encrypted"}),
    "docx_xxe": (make_docx([], document_xml=XXE + "<w:document><w:body>&e;</w:body></w:document>"),
                 "docx", ParseLimits(), {"corrupt"}),
    "docx_laughs": (make_docx([], document_xml=LAUGHS + "<lolz>&lol9;</lolz>"), "docx",
                    ParseLimits(), {"corrupt"}),
    "epub_laughs_opf": (make_epub(["x"], opf=LAUGHS + "<package>&lol9;</package>"), "epub",
                        ParseLimits(), {"corrupt"}),
    "epub_xxe_container": (make_epub(["x"], container=XXE + "<container>&e;</container>"),
                           "epub", ParseLimits(), {"corrupt"}),
    "epub_path_traversal": (
        make_epub(["x"], opf='<?xml version="1.0"?><package><manifest><item id="a" '
                  'href="../../../etc/passwd"/></manifest><spine><itemref idref="a"/></spine>'
                  "</package>"),
        "epub", ParseLimits(), {"corrupt"},
    ),
    "docx_not_zip": (b"PK\x03\x04" + b"\x00" * 100, "docx", ParseLimits(), {"corrupt"}),
    "docx_truncated": (make_docx(["абв " * 300])[:300], "docx", ParseLimits(), {"corrupt"}),
    "pdf_truncated": (make_pdf(LATIN)[:120], "pdf", ParseLimits(), {"corrupt", "empty_text"}),
    "pdf_garbage": (b"%PDF-1.4\n" + bytes(range(256)) * 20, "pdf", ParseLimits(),
                    {"corrupt", "empty_text"}),
    "txt_binary": (bytes(_rng.randrange(256) for _ in range(5000)), "txt", ParseLimits(),
                   {"corrupt"}),
    "txt_nul": (b"\x00\x01\x02" * 500 + "слово ".encode() * 50, "txt", ParseLimits(), {"corrupt"}),
    "empty": (b"", "txt", ParseLimits(), {"empty_text"}),
    "few_letters": (b"1 2 3 4 5 !!! ...", "txt", ParseLimits(), {"empty_text"}),
    "too_large": (b"a" * 2000, "txt", ParseLimits(max_bytes=1000), {"too_large"}),
    "too_much_text": ("слово ".encode() * 2000, "txt", ParseLimits(max_text_chars=5000),
                      {"too_large"}),
    "unsupported": (b"text", "rtf", ParseLimits(), {"unsupported_format"}),
}  # fmt: skip


def _encrypted_pdf() -> bytes:
    from pypdf import PdfReader, PdfWriter

    w = PdfWriter(clone_from=PdfReader(io.BytesIO(make_pdf(LATIN))))
    w.encrypt("a8-password")
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def _many_pages(n: int) -> bytes:
    from pypdf import PdfWriter

    w = PdfWriter()
    for _ in range(n):
        w.add_blank_page(100, 100)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


@pytest.mark.parametrize("name", list(BAD_FILES))
def test_bad_files_have_reason_codes(name: str) -> None:
    """Кр.3, §6.4: опасный/повреждённый/слишком большой файл — `ParseError` с кодом под CHECK
    0011 (без текста), не другое исключение и не зависание."""
    data, fmt, limits, codes = BAD_FILES[name]
    with pytest.raises(ParseError) as e:
        parse(data, fmt, limits)
    assert e.value.code in codes, (name, e.value.code)
    assert CODE.match(e.value.code)


def test_encrypted_and_many_pages_pdf() -> None:
    """PDF с паролем — `encrypted`; страниц больше предела — `too_many_pages`."""
    with pytest.raises(ParseError) as e:
        parse(_encrypted_pdf(), "pdf")
    assert e.value.code == "encrypted"
    with pytest.raises(ParseError) as e2:
        parse(_many_pages(30), "pdf", ParseLimits(max_pages=20))
    assert e2.value.code == "too_many_pages"


def test_epub_xhtml_entities_not_expanded() -> None:
    """XHTML главы с DTD и «миллиардом смехов» — сущности не раскрываются (текст не растёт)."""
    chapter = LAUGHS + "<html><body><p>&lol9; текст главы " + "слово " * 60 + "</p></body></html>"
    buf = io.BytesIO(make_epub([chapter]))
    try:
        out = parse(buf.getvalue(), "epub")
    except ParseError as e:
        assert e.code in ("corrupt", "zip_bomb")
    else:
        assert len(out) < 10_000 and "lollol" not in out


def test_binary_high_bytes_under_txt_known_limitation() -> None:
    """Фиксирует поведение: «мусор» только из байтов 0xC0–0xFF декодируется как CP1251 (буквы) и
    принимается как текст — простая проверка управляющих символов его не отличает (О-1)."""
    data = bytes(_rng.randrange(0xC0, 0x100) for _ in range(3000))
    out = parse(data, "txt")
    assert len(out) == 3000


def test_isolated_parse_success_and_timeout() -> None:
    """Кр.3: разбор в отдельном процессе — успех; тайм-аут — `timeout` (процесс убит)."""
    data = book(3).encode()
    assert "начинает книгу" in parse_isolated(data, "txt", ParseLimits(timeout_s=30))
    with pytest.raises(ParseError) as e:
        parse_isolated(_many_pages(3000), "pdf", ParseLimits(timeout_s=0.05, max_pages=10**6))
    assert e.value.code == "timeout"


# =============================================================== 2. Разбиение


@pytest.mark.parametrize("seed", range(8))
def test_chunking_covers_every_sentence(seed: int) -> None:
    """§6.4: каждое предложение (после нормализации) — хотя бы в одном фрагменте, в порядке
    текста; фрагмент ≤ `max_chars`; перекрытие внутри абзаца — хвост повторяется в начале
    следующего; длинное «предложение» режется по словам без потерь."""
    rng = random.Random(seed)  # noqa: S311
    params = ChunkParams(target_chars=rng.choice((300, 800)), max_chars=1200, overlap_chars=150)
    paras = []
    for _ in range(rng.randint(3, 15)):
        sents = [sentence(rng, rng.randint(3, 40)) for _ in range(rng.randint(1, 12))]
        if rng.random() < 0.2:
            sents.append(" ".join(rng.choice(WORDS) for _ in range(400)) + ".")  # 2–3 тыс. знаков
        paras.append(" ".join(sents))
    raw = "\n\n".join(paras).replace("тишина", "ти-\nшина")  # перенос на конце строки
    chunks = split_chunks(raw, params)
    assert all(0 < len(c) <= params.max_chars for c in chunks)
    joined_words = " ".join(chunks).split()
    pos = 0
    for para in normalize_text(raw):
        for word in para.split():
            # каждое слово текста встречается дальше по порядку (перекрытие — повторы)
            pos = joined_words.index(word, pos) if word in joined_words[pos:] else -1
            assert pos >= 0, word
    assert (
        "ти- шина" not in " ".join(chunks) and "тишина" in " ".join(chunks)
    ) or "тиши" not in raw


def test_overlap_within_paragraph() -> None:
    """Перекрытие: в длинном абзаце следующий фрагмент начинается с хвоста предыдущего."""
    para = " ".join(f"Предложение номер {i} про тихий вечер у реки." for i in range(60))
    chunks = split_chunks(para, ChunkParams(target_chars=400, max_chars=600, overlap_chars=150))
    assert len(chunks) > 2
    shared = sum(1 for a, b in itertools.pairwise(chunks) if b.split(".")[0] in a)
    assert shared >= len(chunks) - 2


def test_normalization_soft_hyphen_and_zero_width() -> None:
    """Мягкие переносы и символы нулевой ширины убираются при нормализации."""
    assert normalize_text("сло­во и​ещё\n\nабзац") == ["слово иещё", "абзац"]


# =============================================================== 3. Метки


@dataclass
class TagModel:
    answer: Any = None
    model: str = "tag-stub"
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def complete_json(self, *, system: str, messages: Sequence[ChatMessage],
                            schema: Mapping[str, Any], max_tokens: int = 16000) -> StructuredResult:  # fmt: skip
        payload = json.loads(messages[-1].content)
        self.calls.append(payload)
        a = self.answer(payload) if callable(self.answer) else self.answer
        if isinstance(a, BaseException):
            raise a
        return StructuredResult(data=a, model=self.model, usage=Usage(), latency_s=0, cost_usd=0)

    async def stream_text(self, **kw: Any) -> Any:  # pragma: no cover
        raise NotImplementedError
        yield ""


def _tag_models(m: TagModel) -> AgentModels:
    return AgentModels(m, m, m, m, m, m, summary=m)


async def test_tags_vocabulary_and_failures() -> None:
    """§6.4: темы — только из словаря (вне словаря отброшены, не больше 3), уровень 1–5 (иначе
    пусто, `True` — не уровень), чужой ref игнорируется; сбой модели и отсутствие модели — пустые
    метки; во вход модели — только тексты фрагментов и подход."""
    chunks = [f"фрагмент {i}" for i in range(10)]

    def ans(payload: dict[str, Any]) -> dict[str, Any]:
        refs = [f["ref"] for f in payload["fragments"]]
        out = [
            {
                "ref": refs[0],
                "topics": ["деньги", "космос", "деньги", "быт", "дети", "ссора"],
                "level": 2,
            },
            {"ref": refs[1], "topics": ["ревность"], "level": 7},
        ]
        if len(refs) > 2:
            out.append({"ref": refs[2], "topics": [], "level": True})
        out.append({"ref": "f999", "topics": ["быт"], "level": 1})
        return {"tags": out}  # fmt: skip

    m = TagModel(answer=ans)
    tags = await tag_chunks(_tag_models(m), chunks, approach="ННО-a8")
    assert tags[0].topics == ("деньги", "быт", "дети") and tags[0].level == 2
    assert tags[1].topics == ("ревность",) and tags[1].level is None
    assert tags[2].level is None and tags[3].topics == () and tags[3].level is None
    assert all(set(t.topics) <= set(TOPICS) for t in tags)
    assert m.calls and set(m.calls[0]) == {"approach", "fragments"}
    assert m.calls[0]["approach"] == "ННО-a8"
    assert all(set(f) == {"ref", "text"} for c in m.calls for f in c["fragments"])
    bad = await tag_chunks(_tag_models(TagModel(answer=LLMUnavailable("x"))), chunks)
    assert all(t.topics == () and t.level is None for t in bad)
    assert all(t.topics == () for t in await tag_chunks(None, chunks))


# =============================================================== 4. Воркер (БД)


async def upload(
    engine: AsyncEngine, data: bytes, fmt: str, approach: str | None = None
) -> uuid.UUID:
    async with engine.begin() as c:
        await c.execute(text("SET LOCAL ROLE feelings_admin"))
        return uuid.UUID(str((await c.execute(
            text("SELECT admin_kb_upload('Выдуманная книга', 'A8', :ap, 'выдумано для тестов', "
                 ":f, :d)"), {"ap": approach, "f": fmt, "d": data})).scalar_one()))  # fmt: skip


async def doc_state(engine: AsyncEngine, doc: uuid.UUID) -> Any:
    async with engine.connect() as c:
        return (await c.execute(text(
            "SELECT index_status::text AS st, index_error, source_data IS NULL AS wiped, "
            "index_attempts AS att, (SELECT count(*) FROM kb_chunks k WHERE k.document_id = d.id) "
            "AS n FROM kb_documents d WHERE id = :d"), {"d": doc})).one_or_none()  # fmt: skip


async def block_others(engine: AsyncEngine, keep: Sequence[uuid.UUID]) -> None:
    """Материалы других тестов не мешают захвату: им — долгая аренда."""
    async with engine.begin() as c:
        await c.execute(text(
            "UPDATE kb_documents SET index_lease_token = gen_random_uuid(), "
            "index_lease_until = now() + interval '1 day' WHERE index_status IN ('uploaded', "
            "'indexing') AND NOT (id = ANY(CAST(:k AS uuid[])))"), {"k": list(keep)})  # fmt: skip


def kbw(engine: AsyncEngine, **kw: Any) -> KbWorker:
    settings = KbSettings(isolate=kw.pop("isolate", False), **kw)
    return KbWorker(worker_sm=make_sessionmaker(engine), models=kw.pop("models", None),
                    embedder=HashingEmbedder(), settings=settings)  # fmt: skip


async def test_worker_indexes_and_wipes_source(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Кр.3–4: четыре формата индексируются (фрагменты, векторы модели эмбеддера), исходник
    стёрт; плохой файл — `failed` с кодом, исходник стёрт, воркер не падает; журнал без текстов."""
    caplog.set_level(logging.DEBUG)
    txt = book(6)
    marker = txt.split()[0]
    docs = {
        "txt": await upload(engine, txt.encode(), "txt"),
        "docx": await upload(engine, make_docx(book(3).split("\n\n")), "docx"),
        "epub": await upload(engine, make_epub(book(2).split("\n\n")), "epub"),
        "pdf": await upload(engine, make_pdf([*LATIN, uuid.uuid4().hex]), "pdf"),
        "bad": await upload(engine, _bomb_zip(3_000_000), "docx"),
    }
    await block_others(engine, list(docs.values()))
    out = await kbw(engine, batch=10, isolate=True).run_once()
    assert len(out) == 5
    for name, d in docs.items():
        st = await doc_state(engine, d)
        assert st.wiped, name
        if name == "bad":
            assert (st.st, st.index_error, st.n) == ("failed", "zip_bomb", 0)
        else:
            assert st.st == "indexed" and st.n > 0, name
    async with engine.connect() as c:
        models = (await c.execute(text("SELECT DISTINCT embedding_model FROM kb_chunks WHERE "
                                       "document_id = :d"), {"d": docs["txt"]})).scalars().all()  # fmt: skip
    assert models == [HashingEmbedder.MODEL]
    assert marker not in caplog.text and "invented guide" not in caplog.text


async def test_worker_two_copies_lease_catchup_attempts(engine: AsyncEngine) -> None:
    """Кр.4: две копии одновременно — одна индексация; «упавшая» копия (захват без завершения) —
    по истечении аренды материал захватывается снова (attempts 2) и индексируется; попыток
    больше предела — `attempts_exhausted`."""
    d1 = await upload(engine, book(4).encode(), "txt")
    await block_others(engine, [d1])
    res = await asyncio.gather(kbw(engine).run_once(), kbw(engine).run_once())
    assert sorted(len(r) for r in res) == [0, 1]
    assert (await doc_state(engine, d1)).st == "indexed"
    d2 = await upload(engine, book(4).encode(), "txt")
    await block_others(engine, [d2])
    sm = make_sessionmaker(engine)
    [crashed] = await kb_work.claim_index(sm, lease=timedelta(seconds=1), limit=5)
    assert crashed.document_id == d2 and crashed.attempts == 1
    assert await kbw(engine).run_once() == []  # аренда жива — никто не берёт
    await asyncio.sleep(1.2)
    [ok] = await kbw(engine).run_once()
    st = await doc_state(engine, d2)
    assert (ok.status, st.st, st.att) == ("indexed", "indexed", 2)
    d3 = await upload(engine, book(4).encode(), "txt")
    await block_others(engine, [d3])
    for _ in range(2):
        await kb_work.claim_index(sm, lease=timedelta(seconds=1), limit=5)
        await asyncio.sleep(1.1)
    [ex] = await kbw(engine, max_attempts=2).run_once()
    st3 = await doc_state(engine, d3)
    assert (ex.status, ex.code, st3.st, st3.index_error, st3.wiped) == (
        "failed", "attempts_exhausted", "failed", "attempts_exhausted", True)  # fmt: skip


async def test_worker_document_deleted_during_indexing(engine: AsyncEngine) -> None:
    """Кр.4: администратор удалил материал во время индексации — работа брошена (`lost`),
    фрагментов и материала нет, воркер не падает."""
    d = await upload(engine, book(5).encode(), "txt")
    await block_others(engine, [d])

    class DeletingEmbedder(HashingEmbedder):
        async def embed(self, texts: Sequence[str]) -> list[list[float]]:
            async with engine.begin() as c:
                await c.execute(text("SET LOCAL ROLE feelings_admin"))
                await c.execute(text("SELECT admin_kb_delete(:d)"), {"d": d})
            return await super().embed(texts)

    w = KbWorker(worker_sm=make_sessionmaker(engine), embedder=DeletingEmbedder(),
                 settings=KbSettings(isolate=False))  # fmt: skip
    [out] = await w.run_once()
    assert out.status == "lost"
    assert await doc_state(engine, d) is None
    async with engine.connect() as c:
        n = (await c.execute(text("SELECT count(*) FROM kb_chunks WHERE document_id = :d"),
                             {"d": d})).scalar_one()  # fmt: skip
    assert n == 0


# =============================================================== 5. Поиск


TOPIC_WORDS = {
    "деньги": "деньги бюджет зарплата расходы кредит покупки траты накопления",
    "ревность": "ревность подозрения телефон переписка измена тревога доверие проверки",
    "дети": "дети школа уроки воспитание сон игрушки садик режим",
}


async def kb_corpus(engine: AsyncEngine) -> tuple[uuid.UUID, str]:
    """Выдуманный корпус: три темы по 6 фрагментов; фрагмент «<тема> 0» — уровень 3, остальные 2."""
    approach = f"a8-approach-{uuid.uuid4().hex[:6]}"
    rng = random.Random(5)  # noqa: S311
    paras = []
    for topic, words in TOPIC_WORDS.items():
        ws = words.split()
        for i in range(6):
            paras.append(f"{topic.capitalize()} {i}. " + " ".join(rng.choice(ws) for _ in range(30))
                         + f". {approach}")  # fmt: skip

    def levels(payload: dict[str, Any]) -> dict[str, Any]:
        out = []
        for f in payload["fragments"]:
            topic = next(t for t in TOPIC_WORDS if f["text"].lower().startswith(t))
            num = int(f["text"].split(".")[0].split()[-1])
            out.append({"ref": f["ref"], "topics": [topic], "level": 3 if num == 0 else 2})
        return {"tags": out}

    d = await upload(engine, "\n\n".join(paras).encode(), "txt", approach=approach)
    await block_others(engine, [d])
    w = KbWorker(worker_sm=make_sessionmaker(engine), models=_tag_models(TagModel(answer=levels)),
                 embedder=HashingEmbedder(),
                 settings=KbSettings(isolate=False, chunking=ChunkParams(target_chars=150,
                                                                         max_chars=400)))  # fmt: skip
    [out] = await w.run_once()
    assert out.status == "indexed" and out.chunks == 18
    uid = uuid.uuid4()
    async with engine.begin() as c:
        await c.execute(text("INSERT INTO users (id, telegram_id) VALUES (:i, :t)"),
                        # 2.6 р1 (A3): id Telegram — случайный, не из зерна корпуса: корпус
                        # строится в двух тестах, одно зерно давало одинаковый id (23505).
                        {"i": uid, "t": 790_000_000_000 + uuid.uuid4().int % 10**9})  # fmt: skip
    return uid, approach


async def test_search_three_to_five_on_topic(engine: AsyncEngine, sessionmaker: SM) -> None:
    """Кр.1 (RoadMap): по теме реплики — 3–5 фрагментов только этой темы (выдуманный корпус из
    трёх тем); реплика не по теме — порог отсекает всё."""
    uid, approach = await kb_corpus(engine)
    emb = HashingEmbedder()
    for topic, words in TOPIC_WORDS.items():
        [q] = await emb.embed([" ".join(words.split()[:4])])
        kb = DbKnowledgeBase(sessionmaker, uid, emb.model, approach=approach)
        found = await kb.search("q", vector=q, limit=KNOWLEDGE_LIMIT)
        assert KNOWLEDGE_MIN <= len(found) <= KNOWLEDGE_LIMIT, (topic, len(found))
        assert all(f.text.lower().startswith(topic) for f in found), topic
    [off] = await emb.embed(["космический корабль орбита ракета"])
    kb = DbKnowledgeBase(sessionmaker, uid, emb.model, approach=approach)
    assert await kb.search("q", vector=off, limit=5) == []


async def test_search_with_level_fills_to_three_on_topic(
    engine: AsyncEngine, sessionmaker: SM
) -> None:
    """Карточка 2.6 («уровень проблемы — фильтр с добором до 3»), кр.1: с известным уровнем —
    сначала фрагменты этого уровня, недобор до 3 — без фильтра уровня, всё по теме. Сейчас добор
    проверяется до порога расстояния: фрагменты нужного уровня **других тем** засчитываются в
    минимум, затем отсекаются порогом — остаётся 1 фрагмент вместо 3–5."""
    uid, approach = await kb_corpus(engine)
    emb = HashingEmbedder()
    for topic, words in TOPIC_WORDS.items():
        [q] = await emb.embed([" ".join(words.split()[:4])])
        lvl = DbKnowledgeBase(sessionmaker, uid, emb.model, approach=approach, level=3)
        got = await lvl.search("q", vector=q, limit=KNOWLEDGE_LIMIT)
        assert KNOWLEDGE_MIN <= len(got) <= KNOWLEDGE_LIMIT, (topic, len(got))
        assert all(f.text.lower().startswith(topic) for f in got)
        assert got[0].text.lower().startswith(f"{topic} 0")


# =============================================================== 6. Запрет цитирования

FRAGMENT = (
    "Когда партнёр говорит о своей обиде, попробуйте сначала повторить его слова своими "
    "словами и только потом объяснять, почему вы поступили именно так, а не иначе."
)
FWORDS = normalize(FRAGMENT).split()


def quote(n: int, start: int = 2) -> str:
    return " ".join(FRAGMENT.split()[start : start + n])


def _has_long_quote(text: str, k: int = QUOTE_WORDS) -> bool:
    words = normalize(text).split()
    grams = {tuple(FWORDS[i : i + k]) for i in range(len(FWORDS) - k + 1)}
    return any(tuple(words[i : i + k]) in grams for i in range(len(words) - k + 1))


VARIANTS = {
    "plain": quote(14),
    "upper": quote(14).upper(),
    "punct": " — ".join(quote(14).split()),
    "yo": quote(14).replace("е", "ё"),
    "newlines": "\n".join(quote(14).split()),
}


@pytest.mark.parametrize("variant", list(VARIANTS))
@pytest.mark.parametrize("seed", range(5))
def test_guard_blocks_quote_any_split(variant: str, seed: int) -> None:
    """§6.4, Б-67: 10+ слов подряд из фрагмента (регистр, пунктуация, «ё», переводы строк) не
    доходят до пользователя ни одним куском при любой нарезке потока; показанное — начало
    ответа + безопасная фраза."""
    rng = random.Random(seed)  # noqa: S311
    answer = f"Слышу тебя. Важно вот что: {VARIANTS[variant]}. И ещё немного своего текста."
    g = QuoteGuard(QuoteIndex([FRAGMENT]))
    shown = []
    i = 0
    while i < len(answer):
        n = rng.randint(1, 9)
        shown.append(g.feed(answer[i : i + n]))
        i += n
    shown.append(g.finish())
    out = "".join(shown)
    assert g.blocked and not _has_long_quote(out), out
    # Придержка: показанное не содержит и начала цитаты (замена — с первого её слова).
    assert not _has_long_quote(out, k=4), out
    assert out == g.shown and out.endswith(SAFE_TAIL) and out.startswith("Слышу тебя.")


def test_short_matches_pass_and_no_delay_without_fragments() -> None:
    """9 слов подряд — проходят без изменений; без фрагментов в контексте поток не
    придерживается (каждый кусок отдаётся сразу)."""
    answer = f"Скажу так: {quote(QUOTE_WORDS - 1)}, но своими словами."
    g = QuoteGuard(QuoteIndex([FRAGMENT]))
    out = g.feed(answer) + g.finish()
    assert out == answer and not g.blocked
    g2 = QuoteGuard(QuoteIndex([]))
    for piece in ("Один ", "два ", "три"):
        assert g2.feed(piece) == piece


async def test_stream_and_nonstream_never_show_quote() -> None:
    """Через поток 2.1 (`stream_reply`) и `dialog.reply`: цитата не доходит ни в `delta`, ни в
    `Done.reply`; склейка `delta` == `Done.reply`; флаг `quote_blocked`."""
    reply = f"Понимаю. {quote(15).upper()}! Давай подумаем вместе."
    ctx = OwnerContext("Аня", "Гоша", knowledge=(FRAGMENT,))
    dlg = (Turn("owner", "Что делать, когда он обижается?"),)
    agent = FakeModel(stream=_chunks(_answer(reply), size=3))
    events = [e async for e in stream_reply(models_of(agent, FakeModel()), ctx, dlg)]
    shown = "".join(e.text for e in events if isinstance(e, Delta))
    done = events[-1]
    assert isinstance(done, Done) and done.quote_blocked
    assert not _has_long_quote(shown) and shown.strip() == done.reply.strip()

    class DialogModel(TagModel):
        async def complete_json(self, **kw: Any) -> StructuredResult:
            props = set(kw["schema"]["properties"])
            data = (_answer(reply) if "draft_ready" in props
                    else {"category": "none", "secondary": []})  # fmt: skip
            return StructuredResult(data=data, model="x", usage=Usage(), latency_s=0, cost_usd=0)

    res = await dialog.reply(_tag_models(DialogModel()), ctx, dlg, with_draft=False)
    assert not _has_long_quote(res.reply) and SAFE_TAIL in res.reply


@pytest.mark.parametrize(
    "evasion",
    [
        "soft_hyphen",
        "zero_width",
    ],
)
def test_invisible_chars_do_not_bypass_guard(evasion: str) -> None:
    """§6.4: невидимые символы внутри слов (мягкий перенос U+00AD, нулевая ширина U+200B) не
    меняют того, что видит пользователь, — цитата должна ловиться. Нормализация фрагментов при
    разбиении их убирает (`normalize_text`), а проверка ответа (`quotes._spans`) — нет: слово
    разрезается на два, 10-граммы не совпадают."""
    ch = "­" if evasion == "soft_hyphen" else "​"
    q = " ".join(w[:2] + ch + w[2:] if len(w) > 3 else w for w in quote(14).split())
    assert find_quote(f"Вот: {q}.", QuoteIndex([FRAGMENT])) is not None


def test_letter_spaced_quote_known_limitation() -> None:
    """Фиксирует поведение: цитата «по буквам через пробел» проверкой слов не ловится (О-2)."""
    q = " ".join(" ".join(w) for w in quote(12).split())
    assert find_quote(q, QuoteIndex([FRAGMENT])) is None


# =============================================================== 7. Админ-API

ADMIN_TOKEN = "a8-admin-token-" + "k" * 40
SESSION_SECRET = "a8-kb-session-secret-" + "s" * 24


@pytest.fixture
async def admin(engine: AsyncEngine, sessionmaker: SM) -> AsyncIterator[httpx.AsyncClient]:
    def settings() -> Settings:
        return Settings(
            telegram_bot_token=SecretStr("777006:A8-kb-bot-token-not-real"),
            session_secret=SecretStr(SESSION_SECRET),
            session_ttl_s=600,
            bot_username="A8_Kb_bot",
            admin_token_sha256=hashlib.sha256(ADMIN_TOKEN.encode()).hexdigest(),
            admin_database_url="postgresql+asyncpg://admin@db.invalid/none",
            env="test",
        )

    prev = (app.state.sessionmaker, getattr(app.state, "admin_sessionmaker", None))
    app.state.sessionmaker = sessionmaker
    app.state.admin_sessionmaker = make_sessionmaker(engine)
    app.dependency_overrides[get_settings] = settings
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                 base_url="http://a8") as c:  # fmt: skip
        yield c
    app.dependency_overrides.clear()
    app.state.sessionmaker, app.state.admin_sessionmaker = prev


H = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
PATH = "/api/v1/admin/kb/documents"


def _form(**kw: Any) -> dict[str, str]:
    base = {"title": "Выдуманная", "rights_basis": "выдумано A8", "approach": "a8"}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


async def test_admin_api_codes(admin: httpx.AsyncClient, engine: AsyncEngine,
                               caplog: pytest.LogCaptureFixture) -> None:  # fmt: skip
    """Кр.5, АР-39, контракт: 401 без токена/с неверным; 403 с сессией Mini App; 202 загрузка;
    409 дубль; 413 больше 20 МиБ; 415 формат не тот (расширение ≠ содержимое); 422 нет
    `rights_basis`/названия, лишнее поле; `DELETE` — 204/404; журнал без токенов и текстов."""
    caplog.set_level(logging.DEBUG)
    body = book(3)
    files = {"file": ("a.txt", body.encode(), "text/plain")}
    assert (await admin.get(PATH)).status_code == 401
    assert (await admin.get(PATH, headers={"Authorization": "Bearer nope"})).status_code == 401
    from app.core.session import issue_token

    uid = uuid.uuid4()
    async with engine.begin() as c:
        await c.execute(text("INSERT INTO users (id, telegram_id) VALUES (:i, :t)"),
                        {"i": uid, "t": 791_000_000_000 + _rng.randrange(10**9)})  # fmt: skip
    user_tok = issue_token(uid, SESSION_SECRET, 600)
    r = await admin.get(PATH, headers={"Authorization": f"Bearer {user_tok}"})
    assert r.status_code == 403 and r.json()["code"] == "admin_only"
    r = await admin.post(PATH, headers={"Authorization": f"Bearer {user_tok}"}, data=_form(),
                         files=files)  # fmt: skip
    assert r.status_code == 403
    r = await admin.post(PATH, headers=H, data=_form(), files=files)
    assert r.status_code == 202, r.text
    doc = r.json()
    assert doc["index_status"] == "uploaded" and doc["source_format"] == "txt"
    assert (await admin.post(PATH, headers=H, data=_form(), files=files)).status_code == 409
    for bad in (_form(rights_basis=None), _form(rights_basis="   "), _form(title=None),
                {**_form(), "extra": "x"}):  # fmt: skip
        r = await admin.post(PATH, headers=H, data=bad,
                             files={"file": ("b.txt", book(2).encode(), "text/plain")})  # fmt: skip
        assert r.status_code == 422, bad
    for name, data in (("x.pdf", b"not a pdf at all"), ("x.docx", book(1).encode()),
                       ("x.exe", b"MZ" + b"\x00" * 100), ("x.txt", b"\x00\x01binary")):  # fmt: skip
        r = await admin.post(PATH, headers=H, data=_form(), files={"file": (name, data, "x/y")})
        assert r.status_code == 415, name
    big = b"a" * (20 * 1024 * 1024 + 1)
    r = await admin.post(
        PATH, headers=H, data=_form(), files={"file": ("big.txt", big, "text/plain")}
    )
    assert r.status_code == 413 and r.json()["code"] == "file_too_large"
    listing = (await admin.get(PATH, headers=H)).json()
    assert any(d["id"] == doc["id"] for d in listing)
    assert all("source_data" not in d for d in listing)
    assert (await admin.delete(f"{PATH}/{doc['id']}", headers=H)).status_code == 204
    assert (await admin.delete(f"{PATH}/{doc['id']}", headers=H)).status_code == 404
    assert (await admin.delete(f"{PATH}/not-a-uuid", headers=H)).status_code == 404
    logs = caplog.text
    assert ADMIN_TOKEN not in logs and user_tok not in logs and body.split()[0] not in logs


async def test_admin_chunked_body_limit(admin: httpx.AsyncClient) -> None:
    """Предел тела без `Content-Length` (chunked): больше 21 МиБ — 413 без чтения целиком."""

    async def gen() -> AsyncIterator[bytes]:
        boundary = b"--a8b\r\n"
        yield boundary + b'Content-Disposition: form-data; name="file"; filename="x.txt"\r\n\r\n'
        for _ in range(23):
            yield b"a" * (1024 * 1024)
        yield b"\r\n--a8b--\r\n"

    r = await admin.post(PATH, headers={**H, "Content-Type": "multipart/form-data; boundary=a8b"},
                         content=gen())  # fmt: skip
    assert r.status_code == 413


async def test_admin_accepts_cp1251_txt(admin: httpx.AsyncClient) -> None:
    """Карточка 2.6 («TXT UTF-8/CP1251»): разборщик понимает CP1251, а `detect_format` при загрузке
    требует UTF-8 — CP1251-файл получает 415 и в индексацию не попадает."""
    r = await admin.post(PATH, headers=H, data=_form(),
                         files={"file": ("cp.txt", book(3).encode("cp1251"), "text/plain")})  # fmt: skip
    assert r.status_code == 202, r.text

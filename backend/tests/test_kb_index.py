"""2.6, этап 1 (A3): база знаний — разбор файлов, разбиение, метки, воркер индексации, поиск,
запрет цитирования.

ТЗ §6.1 п.4, §6.4, §9.1 п.5, §10; ФТ-15; АР-26, АР-30, АР-39; Б-67. Все файлы генерируются в тесте
из **выдуманного** текста (не настоящие книги). Модель и эмбеддер — заглушки (сети нет).
Загрузка — под ``feelings_admin`` (``admin_kb_upload``), индексация — ``feelings_worker``,
поиск — роль входа приложения ``feelings_api`` → ``user_scope``."""

from __future__ import annotations

import asyncio
import io
import uuid
import zipfile
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ai import chat as ai_chat
from app.ai.context import build_context
from app.ai.dialog import OwnerContext, Turn
from app.ai.embeddings import HashingEmbedder
from app.ai.knowledge import DbKnowledgeBase
from app.ai.llm import LLMUnavailable, StructuredResult
from app.ai.models import AgentModels
from app.ai.quotes import SAFE_TAIL, QuoteGuard, QuoteIndex, find_quote, without_quotes
from app.ai.stub import ScriptedProvider, ScriptedStreamProvider, json_chunks
from app.db import make_sessionmaker
from app.kb import parse as kbparse
from app.kb.chunk import ChunkParams, normalize_text, split_chunks
from app.kb.parse import ParseError, ParseLimits, parse, parse_isolated
from app.kb.tag import TOPICS, tag_chunks
from app.repositories import kb_work
from app.workers.kb import KbSettings, KbWorker
from tests.test_chat_context import KEYS, owner_says, person
from tests.test_couple_functions import SM

EMB = HashingEmbedder()
LEASE = timedelta(seconds=2)
LIMITS = ParseLimits(min_letters=20)

# Выдуманные тексты «книги» — по темам (не настоящие книги).
MONEY = [
    "Деньги в паре часто становятся темой ссоры, когда партнёры не обсуждают бюджет заранее.",
    "Разговор о деньгах проще начать с общей цели: на что пара хочет копить и почему это важно.",
    "Когда один партнёр тратит деньги без обсуждения, другой чувствует тревогу и обиду из-за денег.",
    "Совместный бюджет и деньги на личные траты снижают споры о деньгах в паре.",
    "Обсуждая деньги, полезно говорить о своих чувствах и потребностях, а не о вине партнёра.",
]
CHILDREN = [
    "Дети замечают напряжение между родителями, даже если родители молчат при детях.",
    "Договориться о правилах для детей помогает спокойный разговор родителей без детей.",
    "Усталость от детей и быта стоит назвать вслух, прежде чем она станет обидой.",
    "Когда родители спорят о детях, детям важно знать, что их любят оба родителя.",
    "Напряжение родителей дети чувствуют по тону, поэтому родителям стоит мириться при детях.",
]
SLEEP = [
    "Режим сна влияет на настроение: недосып делает реакции резче и короче.",
    "Короткая прогулка вечером и отказ от экрана перед сном улучшают сон.",
]


# --------------------------------------------------------------------------- выдуманные файлы


def make_pdf(pages: list[str]) -> bytes:
    """Минимальный PDF: стандартный шрифт Helvetica, ASCII-текст (кириллице нужен встроенный
    шрифт — для проверки разбора достаточно латиницы)."""
    objs: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font = 3 + 2 * n
    for i, body in enumerate(pages):
        lines = "".join(f"({line}) Tj 0 -16 Td " for line in body.split("\n"))
        stream = f"BT /F1 12 Tf 50 750 Td {lines}ET".encode()
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {4 + 2 * i} 0 R >>".encode()
        )
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def make_docx(
    paragraphs: list[str], extra: dict[str, bytes] | None = None, xml: bytes | None = None
) -> bytes:
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(
        f"<w:p><w:r><w:t>{p}</w:t></w:r><w:r><w:tab/><w:t>.</w:t></w:r></w:p>" for p in paragraphs
    )
    doc = (
        xml
        or f'<?xml version="1.0" encoding="UTF-8"?><w:document {ns}><w:body>{body}</w:body></w:document>'.encode()
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", doc)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def make_epub(chapters: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        items = "".join(
            f'<item id="c{i}" href="text/c{i}.xhtml" media-type="application/xhtml+xml"/>'
            for i in range(len(chapters))
        )
        spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
        z.writestr(
            "OEBPS/content.opf",
            f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf"><manifest>{items}'
            f"</manifest><spine>{spine}</spine></package>",
        )
        for i, ch in enumerate(chapters):
            z.writestr(
                f"OEBPS/text/c{i}.xhtml",
                f"<html><head><title>T</title><script>alert('x')</script></head><body>"
                f"<h1>Глава {i}</h1><p>{ch}</p><p>Второй &amp; абзац главы {i}.</p></body></html>",
            )
    return buf.getvalue()


# --------------------------------------------------------------------------- разбор


def test_parse_four_formats() -> None:
    txt = "\n\n".join(MONEY).encode()
    assert "бюджет" in parse(txt, "txt", LIMITS)
    assert "бюджет" in parse("\n\n".join(MONEY).encode("cp1251"), "txt", LIMITS)
    docx = parse(make_docx(MONEY), "docx", LIMITS)
    assert MONEY[0] in docx and docx.count("\n\n") >= len(MONEY) - 1
    epub = parse(make_epub(CHILDREN), "epub", LIMITS)
    assert CHILDREN[0] in epub and "Второй & абзац" in epub and "alert" not in epub
    pdf = parse(
        make_pdf(
            [
                "Money talks in a couple go easier\nwith a shared budget plan.",
                "Second page text about kids and rules.",
            ]
        ),
        "pdf",
        LIMITS,
    )
    assert "shared budget" in pdf and "Second page" in pdf


@pytest.mark.parametrize(
    ("data", "fmt", "limits", "code"),
    [
        (b"x" * 2000, "txt", ParseLimits(max_bytes=1000), "too_large"),
        (b"%PDF-1.4\n garbage without objects", "pdf", LIMITS, "corrupt"),
        (b"not a zip at all", "docx", LIMITS, "corrupt"),
        (b"not a zip at all", "epub", LIMITS, "corrupt"),
        (b"\x00\x01\x02\x03" * 100, "txt", LIMITS, "corrupt"),
        (b"12345 67890 !!! ???", "txt", LIMITS, "empty_text"),
        (b"text", "odt", LIMITS, "unsupported_format"),
        (b"", "txt", LIMITS, "empty_text"),
    ],
)
def test_parse_rejects_with_code(data: bytes, fmt: str, limits: ParseLimits, code: str) -> None:
    with pytest.raises(ParseError) as ei:
        parse(data, fmt, limits)
    assert ei.value.code == code


def test_pdf_encrypted_and_too_many_pages() -> None:
    from pypdf import PdfReader, PdfWriter

    plain = make_pdf(["Some fictional text about feelings and needs."] * 3)
    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(plain)).pages:
        writer.add_page(page)
    writer.encrypt("secret")
    enc = io.BytesIO()
    writer.write(enc)
    with pytest.raises(ParseError) as ei:
        parse(enc.getvalue(), "pdf", LIMITS)
    assert ei.value.code == "encrypted"
    with pytest.raises(ParseError) as ei:
        parse(plain, "pdf", ParseLimits(max_pages=2, min_letters=5))
    assert ei.value.code == "too_many_pages"


def test_zip_bombs_and_xml_entities() -> None:
    bomb = make_docx(MONEY, extra={"word/media/zeros.bin": b"\0" * (5 * 1024 * 1024)})
    with pytest.raises(ParseError) as ei:
        parse(bomb, "docx", LIMITS)
    assert ei.value.code == "zip_bomb"  # степень сжатия > 200
    many = make_docx(MONEY, extra={f"x/{i}.txt": b"a" for i in range(50)})
    with pytest.raises(ParseError) as ei:
        parse(many, "docx", ParseLimits(max_zip_entries=20, min_letters=20))
    assert ei.value.code == "zip_bomb"
    big = make_epub(["текст " * 20000])
    with pytest.raises(ParseError) as ei:
        parse(big, "epub", ParseLimits(max_unzipped_bytes=50_000, min_letters=20))
    assert ei.value.code == "zip_bomb"
    laughs = (
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'
        b'<w:document xmlns:w="w"><w:body><w:p><w:r><w:t>&lol2;</w:t></w:r></w:p></w:body></w:document>'
    )
    with pytest.raises(ParseError) as ei:
        parse(make_docx([], xml=laughs), "docx", LIMITS)
    assert ei.value.code == "corrupt"  # DTD и сущности не разбираются (defusedxml)


def test_parse_isolated_ok_and_timeout() -> None:
    data = "\n\n".join(MONEY).encode()
    assert "бюджет" in parse_isolated(data, "txt", LIMITS)
    with pytest.raises(ParseError) as ei:
        parse_isolated(data, "txt", ParseLimits(timeout_s=0.0001, min_letters=20))
    assert ei.value.code == "timeout"
    with pytest.raises(ParseError) as ei:
        parse_isolated(b"garbage", "pdf", LIMITS)
    assert ei.value.code == "corrupt"


def test_codes_match_db_pattern() -> None:
    import re

    codes = [
        getattr(kbparse, n)
        for n in dir(kbparse)
        if n.isupper() and isinstance(getattr(kbparse, n), str)
    ]
    codes += ["attempts_exhausted", "too_many_chunks"]
    assert all(re.fullmatch(r"[a-z][a-z0-9_]{0,31}", c) for c in codes if c not in ("pdf",))


# --------------------------------------------------------------------------- разбиение


def test_normalize_and_chunks_respect_limits_and_overlap() -> None:
    raw = "Пере-\nнос слова и\nстрока.­\n\n\n  Второй   абзац.\r\n"
    assert normalize_text(raw) == ["Перенос слова и строка.", "Второй абзац."]
    sentences = [f"Предложение {i} о том, как говорить о чувствах без упрёков." for i in range(60)]
    long_para = " ".join(sentences)
    p = ChunkParams()
    chunks = split_chunks(long_para + "\n\nКороткий финальный абзац о просьбе.", p)
    assert len(chunks) > 3 and all(len(c) <= p.max_chars for c in chunks)
    # перекрытие внутри абзаца: последнее предложение фрагмента начинает следующий
    last = chunks[0].split(". ")[-1]
    assert chunks[1].startswith(last.rstrip(".")) or last in chunks[1]
    assert chunks[-1].endswith("Короткий финальный абзац о просьбе.")
    giant = "слово " * 1000
    assert all(len(c) <= p.max_chars for c in split_chunks(giant, p))
    assert split_chunks("   \n\n  ", p) == []


# --------------------------------------------------------------------------- метки


async def test_tagging_filters_and_falls_back_to_empty() -> None:
    seen: list[dict[str, Any]] = []

    def handler(role: str, payload: dict[str, Any], system: str) -> dict[str, Any]:
        assert role == "kb_tag"
        seen.append(payload)
        return {
            "tags": [
                {"ref": "f1", "topics": ["деньги", "деньги", "конфликт"], "level": 2},
                {"ref": "f2", "topics": ["дети"], "level": 7},
                {"ref": "zz", "topics": ["быт"], "level": 1},
            ]
        }

    tags = await tag_chunks(
        AgentModels.single(ScriptedProvider(handler)), MONEY[:3], approach="ННО"
    )
    assert [(t.topics, t.level) for t in tags] == [
        (("деньги", "конфликт"), 2),
        (("дети",), None),
        ((), None),
    ]
    assert seen[0]["approach"] == "ННО" and [f["text"] for f in seen[0]["fragments"]] == MONEY[:3]
    assert all(t in TOPICS for t in ("деньги", "дети"))

    class Down(ScriptedProvider):
        async def complete_json(self, **kw: Any) -> StructuredResult:
            raise LLMUnavailable("нет сети")

    empty = await tag_chunks(AgentModels.single(Down(handler)), MONEY[:2])
    assert [(t.topics, t.level) for t in empty] == [((), None), ((), None)]
    assert [(t.topics, t.level) for t in await tag_chunks(None, MONEY[:1])] == [((), None)]


# --------------------------------------------------------------------------- воркер


async def upload(engine: AsyncEngine, data: bytes, fmt: str, approach: str = "ННО") -> uuid.UUID:
    async with engine.begin() as c:
        await c.execute(text("SET LOCAL ROLE feelings_admin"))
        value = (
            await c.execute(
                text("SELECT admin_kb_upload(:t, NULL, :ap, 'выдуманный текст для теста', :f, :s)"),
                {
                    "t": f"Выдуманная книга {uuid.uuid4().hex[:6]}",
                    "ap": approach,
                    "f": fmt,
                    "s": data,
                },
            )
        ).scalar_one()
    return uuid.UUID(str(value))


async def claim_doc(engine: AsyncEngine, doc: uuid.UUID, lease: timedelta = LEASE) -> Any:
    wsm = make_sessionmaker(engine)
    for _ in range(50):
        claims = await kb_work.claim_index(wsm, lease=lease, limit=20)
        mine = [c for c in claims if c.document_id == doc]
        if mine:
            return mine[0]
        if not claims:
            return None
    raise AssertionError("слишком много кандидатов")


async def doc_state(engine: AsyncEngine, doc: uuid.UUID) -> Any:
    async with engine.connect() as c:
        return (
            await c.execute(
                text(
                    "SELECT index_status::text AS status, index_error, source_data IS NULL AS gone, "
                    "(SELECT count(*) FROM kb_chunks k WHERE k.document_id = d.id) AS n, "
                    "(SELECT count(*) FROM kb_chunks k WHERE k.document_id = d.id "
                    " AND k.embedding_model IS NOT NULL) AS vec "
                    "FROM kb_documents d WHERE id = :d"
                ),
                {"d": doc},
            )
        ).one()


def worker(engine: AsyncEngine, model: Any = None, **kw: Any) -> KbWorker:
    return KbWorker(
        worker_sm=make_sessionmaker(engine),
        models=AgentModels.single(model) if model is not None else None,
        embedder=EMB,
        settings=KbSettings(lease=LEASE, limits=LIMITS, isolate=kw.pop("isolate", False), **kw),
    )


def tag_handler(topics: list[str], level: int | None) -> Any:
    def handler(role: str, payload: dict[str, Any], system: str) -> dict[str, Any]:
        return {
            "tags": [
                {"ref": f["ref"], "topics": topics, "level": level} for f in payload["fragments"]
            ]
        }

    return handler


async def test_worker_indexes_formats_with_tags_and_vectors(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "ТЕКСТКНИГИ"
    w = worker(engine, ScriptedProvider(tag_handler(["деньги"], 2)), isolate=True)
    caplog.set_level("INFO", logger="app.workers.kb")
    for fmt, data in (
        ("txt", ("\n\n".join(MONEY) + f" {secret} {uuid.uuid4()}").encode()),
        ("docx", make_docx([*MONEY, f"{secret} {uuid.uuid4()}"])),
        ("epub", make_epub([*CHILDREN, f"{secret} {uuid.uuid4()}"])),
        ("pdf", make_pdf([f"Fictional page about money and budget {secret} {uuid.uuid4().hex}"])),
    ):
        doc = await upload(engine, data, fmt)
        claim = await claim_doc(engine, doc)
        out = await w.index(claim)
        assert out.status == "indexed" and out.chunks >= 1, (fmt, out)
        st = await doc_state(engine, doc)
        assert (st.status, st.gone, st.n, st.vec) == ("indexed", True, out.chunks, out.chunks)
        async with engine.connect() as c:
            row = (
                await c.execute(
                    text(
                        "SELECT topic_tags, problem_level, ord FROM kb_chunks WHERE document_id = :d ORDER BY ord LIMIT 1"
                    ),
                    {"d": doc},
                )
            ).one()
        assert (list(row.topic_tags), row.problem_level, row.ord) == (["деньги"], 2, 0)
    assert secret not in caplog.text


async def test_worker_failures_have_codes_and_do_not_crash(engine: AsyncEngine) -> None:
    w = worker(engine)
    cases = [
        (b"%PDF-1.4 broken " + uuid.uuid4().bytes, "pdf", "corrupt"),
        (
            make_docx(MONEY, extra={"z.bin": b"\0" * (5 * 1024 * 1024), "u": uuid.uuid4().bytes}),
            "docx",
            "zip_bomb",
        ),
        (f"1234 5678 {uuid.uuid4().int}".encode(), "txt", "empty_text"),
    ]
    for data, fmt, code in cases:
        doc = await upload(engine, data, fmt)
        claim = await claim_doc(engine, doc)
        out = await w.index(claim)
        assert (out.status, out.code) == ("failed", code)
        st = await doc_state(engine, doc)
        assert (st.status, st.index_error, st.gone, st.n) == ("failed", code, True, 0)


async def test_worker_attempts_exhausted_and_catch_up(engine: AsyncEngine) -> None:
    """Брошенный материал (воркер «упал» после захвата) захватывается снова после аренды —
    догон; попыток больше предела — ``attempts_exhausted``."""
    doc = await upload(engine, ("\n\n".join(SLEEP) + f" {uuid.uuid4()}").encode(), "txt")
    first = await claim_doc(engine, doc)
    assert first.attempts == 1  # «упали» — ничего не завершили
    assert await claim_doc(engine, doc) is None  # аренда действует — вторая копия не берёт
    await asyncio.sleep(LEASE.total_seconds() + 0.2)
    again = await claim_doc(engine, doc)
    assert again.attempts == 2
    out = await worker(engine, max_attempts=1).index(again)
    assert (out.status, out.code) == ("failed", "attempts_exhausted")


async def test_lost_lease_abandons_work(engine: AsyncEngine) -> None:
    doc = await upload(engine, ("\n\n".join(MONEY) + f" {uuid.uuid4()}").encode(), "txt")
    stale = await claim_doc(engine, doc)
    await asyncio.sleep(LEASE.total_seconds() + 0.2)
    fresh = await claim_doc(engine, doc)
    assert (await worker(engine).index(stale)).status == "lost"  # чужая аренда — бросить
    assert (await worker(engine).index(fresh)).status == "indexed"


async def test_run_once_two_copies_no_duplicates(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = await upload(engine, ("\n\n".join(CHILDREN) + f" {uuid.uuid4()}").encode(), "txt")
    real = kb_work.claim_index

    async def only_mine(*a: Any, **kw: Any) -> list[Any]:
        for _ in range(50):
            got = await real(*a, **kw)
            if not got or any(c.document_id == doc for c in got):
                return [c for c in got if c.document_id == doc]
        return []

    monkeypatch.setattr(kb_work, "claim_index", only_mine)
    a, b = worker(engine), worker(engine)
    outs = await asyncio.gather(a.run_once(), b.run_once())
    assert sorted(len(o) for o in outs) == [0, 1]
    st = await doc_state(engine, doc)
    assert st.status == "indexed" and st.n == len(split_chunks("\n\n".join(CHILDREN)))


# --------------------------------------------------------------------------- поиск 3–5 по теме


async def test_search_finds_3_to_5_on_topic(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Критерий RoadMap: по теме реплики — 3–5 подходящих фрагментов (выдуманный корпус, заглушка
    эмбеддингов); фрагменты других тем не попадают; уровень проблемы — фильтр с добором."""
    approach = f"ННО-{uuid.uuid4().hex[:6]}"
    tagged = {"деньги": 2, "дети": 3, "сон": None}
    docs = {}
    for topic, texts in (("деньги", MONEY), ("дети", CHILDREN), ("сон", SLEEP)):
        handler = tag_handler([topic] if topic != "сон" else [], tagged[topic])
        w = worker(
            engine,
            ScriptedProvider(handler),
            chunking=ChunkParams(target_chars=60, max_chars=200, overlap_chars=0),
        )
        doc = await upload(
            engine, ("\n\n".join(texts) + f"\n\n{uuid.uuid4()}").encode(), "txt", approach
        )
        assert (await w.index(await claim_doc(engine, doc))).status == "indexed"
        docs[topic] = doc
    async with engine.connect() as c:
        by_id = {
            str(r.id): r.document_id
            for r in await c.execute(
                text("SELECT id, document_id FROM kb_chunks WHERE document_id = ANY(:d)"),
                {"d": list(docs.values())},
            )
        }
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    query = "Мы опять поссорились из-за денег: он тратит деньги без обсуждения бюджета"
    await owner_says(sessionmaker, p, query)
    kb = DbKnowledgeBase(sessionmaker, p.user_id, EMB.model, approach=approach)
    built = await build_context(
        sessionmaker, KEYS, p.user_id, p.conv, query=query, embedder=EMB, knowledge=kb
    )
    ids = built.stats.knowledge_ids
    assert 3 <= len(ids) <= 5
    assert all(by_id[i] == docs["деньги"] for i in ids)
    # уровень проблемы известен (3 — накопленный конфликт): сначала фрагменты этого уровня
    lvl = DbKnowledgeBase(sessionmaker, p.user_id, EMB.model, approach=approach, level=3)
    [qv] = await EMB.embed(["дети замечают напряжение родителей, родители спорят о детях"])
    got = await lvl.search("x", vector=qv)
    assert 3 <= len(got) <= 5 and all(by_id[f.id] == docs["дети"] for f in got)


# --------------------------------------------------------------------------- запрет цитирования


FRAGMENT = (
    "Когда один партнёр тратит деньги без обсуждения, другой чувствует тревогу и обиду, "
    "и разговор лучше начинать с общей цели пары."
)


def test_find_quote_threshold_and_normalization() -> None:
    idx = QuoteIndex([FRAGMENT])
    quote = "Слышу тебя. Когда один партнёр ТРАТИТ деньги без обсуждения — другой чувствует тревогу и обиду."
    assert find_quote(quote, idx) == quote.index("Когда")
    nine = "Когда один партнёр тратит деньги без обсуждения, другой чувствует. Это тяжело."
    assert find_quote(nine, idx) is None  # 9 слов подряд — ещё не цитата
    own = "Похоже, тебя тревожит, что решения о тратах принимаются без тебя."
    assert find_quote(own, idx) is None
    text_, blocked = without_quotes(quote, idx)
    assert blocked and text_ == "Слышу тебя. " + SAFE_TAIL
    assert not QuoteIndex([])


@pytest.mark.parametrize("step", [1, 4, 17])
def test_quote_guard_never_shows_quote(step: int) -> None:
    idx = QuoteIndex([FRAGMENT])
    reply = "Понимаю, как это задевает. Когда один партнёр тратит деньги без обсуждения, другой чувствует тревогу и обиду. Расскажи подробнее?"
    g = QuoteGuard(idx)
    shown = ""
    for i in range(0, len(reply), step):
        piece = g.feed(reply[i : i + step])
        shown += piece
        assert find_quote(shown, idx) is None
    shown += g.finish()
    assert g.blocked and shown == g.shown and shown.endswith(SAFE_TAIL)
    assert "Понимаю, как это задевает." in shown and "тратит деньги без обсуждения" not in shown


async def test_stream_reply_blocks_quote_before_showing() -> None:
    """В потоке цитата не показывается ни одним куском; итог — показанное + безопасная фраза."""
    reply = "Слышу тебя. Когда один партнёр тратит деньги без обсуждения, другой чувствует тревогу и обиду, и разговор лучше начинать с общей цели пары."
    answer = {
        "level": 2,
        "reply": reply,
        "draft_ready": False,
        "crisis_type": None,
        "crisis_secondary": [],
    }
    p = ScriptedStreamProvider(
        lambda *a: {"category": "none", "secondary": []}, stream=lambda pl: json_chunks(answer, 3)
    )
    ctx = OwnerContext("Вера", "Олег", knowledge=(FRAGMENT,))
    events = [
        e
        async for e in ai_chat.stream_reply(AgentModels.single(p), ctx, (Turn("owner", "деньги"),))
    ]
    done = events[-1]
    assert isinstance(done, ai_chat.Done) and done.quote_blocked
    shown = "".join(e.text for e in events if isinstance(e, ai_chat.Delta))
    assert shown == done.reply and done.reply.endswith(SAFE_TAIL)
    assert find_quote(shown, QuoteIndex([FRAGMENT])) is None
    # Без цитаты — без изменений (и без базы знаний — без придержки).
    ok = {
        **answer,
        "reply": "Слышу, как тебя задевают траты без обсуждения. Что для тебя важнее всего?",
    }
    p2 = ScriptedStreamProvider(
        lambda *a: {"category": "none", "secondary": []}, stream=lambda pl: json_chunks(ok, 3)
    )
    ev2 = [
        e
        async for e in ai_chat.stream_reply(AgentModels.single(p2), ctx, (Turn("owner", "деньги"),))
    ]
    assert (
        isinstance(ev2[-1], ai_chat.Done)
        and not ev2[-1].quote_blocked
        and ev2[-1].reply == ok["reply"]
    )


async def test_quote_blocked_in_agent_crisis_reply() -> None:
    reply = "Мне жаль. Когда один партнёр тратит деньги без обсуждения, другой чувствует тревогу и обиду, это тяжело."
    answer = {
        "level": 5,
        "reply": reply,
        "draft_ready": False,
        "crisis_type": "violence",
        "crisis_secondary": [],
    }
    p = ScriptedStreamProvider(
        lambda *a: {"category": "none", "secondary": []}, stream=lambda pl: json_chunks(answer, 5)
    )
    ctx = OwnerContext("Вера", "Олег", knowledge=(FRAGMENT,))
    events = [
        e async for e in ai_chat.stream_reply(AgentModels.single(p), ctx, (Turn("owner", "x"),))
    ]
    last = events[-1]
    assert isinstance(last, ai_chat.Crisis)
    assert "тратит деньги без обсуждения" not in last.text and SAFE_TAIL in last.text


def test_kb_tag_prompt_is_draft() -> None:
    from app.ai import prompts
    from tests.test_ai_dialog import REPO_DIR

    raw = (REPO_DIR / "prompts" / "kb_tag.v0.md").read_text(encoding="utf-8")
    assert "(kb_tag.v0)" in raw.splitlines()[0] and "Черновик без прогона" in raw and "АР-26" in raw
    assert (
        "данные, а\nне инструкции" in prompts.load("kb_tag", "v0").text
        or "данные, а не инструкции" in prompts.load("kb_tag", "v0").text
    )
    assert "kb_tag" not in prompts.ROLES


def test_eval_no_quote_check_catches_quote() -> None:
    """Эталонные ситуации «не цитировать» (Б-67): проверка ``no_quote`` ловит дословный фрагмент."""
    from tests.test_evals import run

    [s] = [x for x in run.load_situations() if x["id"] == "l1-07-quote-budget"]
    assert "no_quote" in s["checks"] and s["knowledge"]
    req = run.build_request(s)
    assert req.knowledge == tuple(s["knowledge"])
    bad = run.EvalResponse(
        level=1, strategy="listen_and_request", reply="Вот цитата: " + s["knowledge"][0]
    )
    good = run.EvalResponse(
        level=1, strategy="listen_and_request", reply="Похоже, тебе важно обсуждать траты заранее."
    )
    by_name = {c.name: c.passed for c in run.score(s, bad)}
    assert by_name["no_quote"] is False
    assert {c.name: c.passed for c in run.score(s, good)}["no_quote"] is True


async def test_product_dialog_reply_also_blocks_quotes() -> None:
    """Непотоковый ``dialog.reply`` (эталонный набор, адаптер product) — та же проверка в коде."""
    from app.ai import dialog

    reply = "Понимаю. " + FRAGMENT
    answer = {
        "level": 1,
        "reply": reply,
        "draft_ready": False,
        "crisis_type": None,
        "crisis_secondary": [],
    }

    def handler(role: str, payload: dict[str, Any], system: str) -> dict[str, Any]:
        return answer if role == "dialog" else {"category": "none", "secondary": []}

    ctx = OwnerContext("Вера", "Олег", knowledge=(FRAGMENT,))
    result = await dialog.reply(
        AgentModels.single(ScriptedProvider(handler)),
        ctx,
        (Turn("owner", "деньги"),),
        with_draft=False,
    )
    assert result.reply == "Понимаю. " + SAFE_TAIL

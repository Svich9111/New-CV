"""2.4, A8: независимая проверка долговременной памяти (ФТ-12…ФТ-14; ТЗ §2 п.4, п.6, §6.3; АР-31,
АР-36) — миграция 0009, `/memory`, извлечение/слияние/сжатие и воркер (`app.ai.memory`,
`app.repositories.memory_work`, `app.workers.memory`), отпечатки (`app.core.fingerprint`).

Свои заглушки: модель (`MemModel` — извлечение, слияние, сводка профиля; записывает всё, что ушло бы
в модель), эмбеддер (`WordsEmbedder` — мешок слов, без сети), отпечаток для проверки формата —
своя реализация АР-36 (`my_fp`). Хелперы исполнителя (`test_memory_worker.py`) не используются.
Данные заводит суперпользователь; обработка — как в продукте: захват — `feelings_worker`,
запись — `user_scope` (`feelings_app`). Данные выдуманные. Дефекты — `xfail(strict=True)`
(docs/reviews/2.4-test.md)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import unicodedata
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from app.ai import memory as mem_ai
from app.ai.context import build_context
from app.ai.llm import ChatMessage, LLMRefusal, LLMUnavailable, StructuredResult, Usage
from app.ai.models import AgentModels
from app.config import Settings, get_settings
from app.core import crypto
from app.core.fingerprint import (
    FingerprintConfigError,
    FingerprintKeys,
    normalize,
    parse_fingerprint_keys,
)
from app.db import user_scope
from app.main import app
from app.repositories import chat as chat_repo
from app.repositories import memory_work as mw
from app.workers.memory import MemoryWorker, WorkerSettings
from tests.test_chat_a8 import KEYS, _conv, _pmsg, _turn, _user, summary_envelope

SM = async_sessionmaker[AsyncSession]
FPK = os.urandom(32)
FP = FingerprintKeys(FPK)
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
GAP = timedelta(minutes=1)
OLD = datetime.now(UTC) - timedelta(days=3)


def ago(seconds: int) -> datetime:
    return OLD + timedelta(seconds=seconds)


# =============================================================== своя реализация АР-36


def my_norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    value = "".join(c if unicodedata.category(c)[0] in "LN" else " " for c in value)
    return " ".join(value.split())


def my_fp(key: bytes, agent: uuid.UUID, body: str) -> bytes:
    msg = b"memfp1" + b"\x1f" + str(agent).lower().encode() + b"\x1f" + my_norm(body).encode()
    return hmac.new(key, msg, hashlib.sha256).digest()


# =============================================================== заглушки модели и эмбеддера


@dataclass
class MemModel:
    """Поставщик без сети. ``items`` — ответ извлечения (dict-записи или исключение; функция от
    входа — тоже); ``merge`` — ``(same, text)`` или функция; ``profile`` — строка или функция.
    ``hooks`` — корутины, которые выполняются при вызове соответствующей роли (сбой «посреди»)."""

    items: Any = field(default_factory=list)
    merge: Any = (False, "")
    profile: Any = "профиль"
    model: str = "mem-stub"
    calls: list[dict[str, Any]] = field(default_factory=list)
    hooks: dict[str, Callable[[], Awaitable[None]]] = field(default_factory=dict)

    async def complete_json(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        schema: Mapping[str, Any],
        max_tokens: int = 16000,
    ) -> StructuredResult:
        props = set(schema.get("properties", {}))
        kind = "extract" if "items" in props else "merge" if "same" in props else "profile"
        payload = json.loads(messages[-1].content)
        self.calls.append({"kind": kind, "system": system, "payload": payload,
                           "raw": "\n".join(m.content for m in messages)})  # fmt: skip
        hook = self.hooks.pop(kind, None)
        if hook is not None:
            await hook()
        spec = {"extract": self.items, "merge": self.merge, "profile": self.profile}[kind]
        if callable(spec):
            spec = spec(payload)
        if isinstance(spec, BaseException):
            raise spec
        if kind == "extract":
            data: dict[str, Any] = {"items": [_item(x) for x in spec]}
        elif kind == "merge":
            data = {"same": spec[0], "text": spec[1]}
        else:
            data = {"profile": spec}
        return StructuredResult(data=data, model=self.model, usage=Usage(), latency_s=0, cost_usd=0)

    async def stream_text(self, **kw: Any) -> Any:  # pragma: no cover
        raise NotImplementedError
        yield ""

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["kind"] == kind]

    def dump(self) -> str:
        return "\n".join(c["system"] + "\n" + c["raw"] for c in self.calls)


def _item(x: Any) -> dict[str, Any]:
    if isinstance(x, str):
        x = {"text": x}
    return {
        "kind": x.get("kind", "fact"),
        "text": x["text"],
        "event_date": x.get("event_date"),
        "confidence": x.get("confidence", 0.9),
        "partner_ref": x.get("partner_ref"),
    }


def models_of(m: MemModel) -> AgentModels:
    return AgentModels(m, m, m, m, m, m, summary=m)


class WordsEmbedder:
    """Мешок слов (нормализованных): одинаковые слова — близкие векторы. Без сети."""

    model = "a8-words-v1"
    dim = 1024

    def __init__(self) -> None:
        self.inputs: list[str] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.inputs.extend(texts)
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in my_norm(t).split():
                v[int(hashlib.sha256(w.encode()).hexdigest(), 16) % self.dim] += 1.0
            if not any(v):
                v[0] = 1.0
            out.append(v)
        return out


# =============================================================== мир


@dataclass
class P:
    uid: uuid.UUID
    agent: uuid.UUID
    conv: uuid.UUID


async def person(c: AsyncConnection, name: str) -> P:
    uid, agent = await _user(c, name=name, address_form=name, partner_name="Партнёр")
    conv = await _conv(c, agent)
    return P(uid, agent, conv)


async def say(c: AsyncConnection, p: P, author: str, body: str, when: datetime) -> uuid.UUID:
    return await _turn(c, p.conv, author, body, when)


async def add_mem(
    c: AsyncConnection,
    p: P,
    body: str,
    *,
    kind: str = "fact",
    source: str = "owner_dialog",
    key: bytes = FPK,
    when: datetime | None = None,
) -> uuid.UUID:
    mid = uuid.uuid4()
    await c.execute(
        text(
            "INSERT INTO memory_items (id, agent_id, type, source, text_enc, fingerprint, "
            "created_at) VALUES (:i, :a, CAST(:t AS memory_type), CAST(:s AS memory_source), :b, "
            ":f, :w)"
        ),
        {
            "i": mid,
            "a": p.agent,
            "t": kind,
            "s": source,
            "b": KEYS.seal(crypto.MEMORY_TEXT, mid, body),
            "f": my_fp(key, p.agent, body),
            "w": when or ago(0),
        },
    )
    return mid


async def delete_mem(sm: SM, p: P, item: uuid.UUID) -> bool:
    async with user_scope(sm, p.uid) as s:
        return bool(
            (await s.execute(text("SELECT app_delete_memory(:i)"), {"i": item})).scalar_one()
        )


@dataclass
class Row:
    id: uuid.UUID
    type: str
    source: str
    text: str | None
    confidence: float
    deleted: bool
    fingerprint: bytes | None
    source_message_id: uuid.UUID | None
    model: str | None


async def memory_of(engine: AsyncEngine, p: P, *, live_only: bool = True) -> list[Row]:
    async with engine.connect() as c:
        rows = await c.execute(
            text(
                "SELECT id, type::text AS type, source::text AS source, text_enc, confidence, "
                "deleted_at IS NOT NULL AS deleted, fingerprint, source_message_id, "
                "embedding_model FROM memory_items WHERE agent_id = :a ORDER BY created_at, id"
            ),
            {"a": p.agent},
        )
        out = []
        for r in rows:
            body = None if r.text_enc is None else KEYS.open(crypto.MEMORY_TEXT, r.id, r.text_enc)
            out.append(Row(r.id, r.type, r.source, body, float(r.confidence), r.deleted,
                           None if r.fingerprint is None else bytes(r.fingerprint),
                           r.source_message_id, r.embedding_model))  # fmt: skip
    return [x for x in out if not (live_only and x.deleted)]


def worker(
    engine: AsyncEngine, sm: SM, m: MemModel, *, emb: Any = None, fp: FingerprintKeys = FP,
    lease: timedelta = timedelta(minutes=10), max_attempts: int = 5,
) -> MemoryWorker:  # fmt: skip
    return MemoryWorker(
        worker_sm=async_sessionmaker(engine),
        app_sm=sm,
        keys=KEYS,
        fingerprints=fp,
        models=models_of(m),
        embedder=emb,
        settings=WorkerSettings(
            session_gap=GAP,
            lease=lease,
            compact_every=timedelta(minutes=1),
            max_attempts=max_attempts,
        ),
    )


async def claims(engine: AsyncEngine, lease: timedelta = timedelta(minutes=10)) -> list[Any]:
    return await mw.claim_extraction(async_sessionmaker(engine), gap=GAP, lease=lease, limit=100)


async def claim_for(engine: AsyncEngine, p: P, lease: timedelta = timedelta(minutes=10)) -> Any:
    found = [c for c in await claims(engine, lease) if c.agent_id == p.agent]
    return found[0] if found else None


async def extract_for(
    engine: AsyncEngine, sm: SM, p: P, m: MemModel, *, emb: Any = None, fp: FingerprintKeys = FP
) -> Any:
    claim = await claim_for(engine, p)
    assert claim is not None
    return await mem_ai.run_extraction(sm, KEYS, fp, models_of(m), claim, embedder=emb)


async def release_others(engine: AsyncEngine, keep: P) -> None:
    """Чужие агенты прошлых тестов не мешают захвату: их аренды «заняты» надолго."""
    async with engine.begin() as c:
        await c.execute(
            text(
                "UPDATE agents SET memory_lease_token = gen_random_uuid(), memory_lease_kind = "
                "'extract', memory_lease_at = now(), memory_lease_until = now() + interval '1 day' "
                "WHERE id <> :a AND memory_lease_token IS NULL"
            ),
            {"a": keep.agent},
        )


# =============================================================== 1. Отпечаток


def test_fingerprint_format_matches_spec() -> None:
    """АР-36: формат и нормализация (NFKC → casefold → ё→е → не L*/N* → пробел → схлопнуть) —
    своя реализация спецификации совпадает с продуктовой."""
    a = uuid.uuid4()
    samples = [
        "Ёжик в ТУМАНЕ!!!", "ежик, в тумане", "  ЁЖИК\tв\nтумане…  ", "ｆｕｌｌ ｗｉｄｔｈ",
        "Straße", "работает посменно; двое детей", "!!!", "",
    ]  # fmt: skip
    for s in samples:
        assert normalize(s) == my_norm(s), s
        assert FP.of(a, s) == my_fp(FPK, a, s), s
    assert FP.of(a, samples[0]) == FP.of(a, samples[1]) == FP.of(a, samples[2])
    assert FP.of(a, "ｆｕｌｌ ｗｉｄｔｈ") == FP.of(a, "Full Width")
    assert FP.of(a, "любит горы") != FP.of(uuid.uuid4(), "любит горы")  # агент в отпечатке
    assert FP.of(a, "любит горы") != FingerprintKeys(os.urandom(32)).of(a, "любит горы")
    assert FP.of(a, "Любит горы") != FP.of(a, "любит гору")
    assert FPK.hex() not in repr(FP)


def test_fingerprint_keys_config() -> None:
    """АР-36: ключ base64url ≥ 32 байт; пустой, короткий, не base64 — ошибка настройки;
    прежние ключи участвуют только в проверке (`all_of`)."""
    import base64

    good = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    old = base64.urlsafe_b64encode(os.urandom(40)).decode()
    keys = parse_fingerprint_keys(good, [old, " "])
    a = uuid.uuid4()
    assert len(keys.all_of(a, "x")) == 2 and keys.of(a, "x") == keys.all_of(a, "x")[0]
    for bad in ("", "   ", base64.urlsafe_b64encode(os.urandom(31)).decode(), "!!!@@@"):
        with pytest.raises(FingerprintConfigError):
            parse_fingerprint_keys(bad)
    with pytest.raises(FingerprintConfigError):
        parse_fingerprint_keys(good, [base64.urlsafe_b64encode(b"short").decode()])


# =============================================================== 2. Права 0009 по колонкам


async def test_column_privileges_and_deleted_row_immutable(
    engine: AsyncEngine, sessionmaker: SM
) -> None:
    """АР-36 (Б-63): `feelings_app` не пишет вектор, модель, `deleted_at` (42501, в т.ч. копией
    из своей строки) и служебные колонки агента/диалога; удалённую запись не изменит, не
    восстановит, не сотрёт физически; живую — может стереть (слияние)."""
    async with engine.begin() as c:
        p = await person(c, "Права")
        live = await add_mem(c, p, "живая запись")
        gone = await add_mem(c, p, "удалённая запись")
    assert await delete_mem(sessionmaker, p, gone)
    denied = [
        "UPDATE memory_items SET embedding = NULL WHERE id = :i",
        "UPDATE memory_items SET embedding_model = 'x' WHERE id = :i",
        "UPDATE memory_items SET deleted_at = now() WHERE id = :i",
        "UPDATE memory_items SET deleted_at = NULL WHERE id = :i",
        "UPDATE memory_items m SET embedding = o.embedding FROM memory_items o WHERE m.id = :i "
        "AND o.id = :i",
        "INSERT INTO memory_items (agent_id, type, source, text_enc, fingerprint, embedding_model)"
        " VALUES (:a, 'fact', 'owner_dialog', 'x', 'f', 'm')",
        "INSERT INTO memory_items (agent_id, type, source, text_enc, fingerprint, deleted_at)"
        " VALUES (:a, 'fact', 'owner_dialog', 'x', 'f', now())",
        "UPDATE agents SET memory_lease_token = NULL WHERE id = :a",
        "UPDATE agents SET memory_attempts = 0 WHERE id = :a",
        "UPDATE agents SET memory_compacted_at = now() WHERE id = :a",
        "UPDATE agents SET memory_partner_through_at = NULL WHERE id = :a",
        "UPDATE conversations SET memory_through_at = NULL, memory_through_id = NULL "
        "WHERE agent_id = :a",
    ]
    for sql in denied:
        with pytest.raises(DBAPIError) as e:
            async with user_scope(sessionmaker, p.uid) as s:
                await s.execute(text(sql), {"i": live, "a": p.agent})
        assert e.value.orig.sqlstate == "42501", sql  # type: ignore[union-attr]
    async with engine.connect() as c:
        before = tuple(
            (await c.execute(text("SELECT * FROM memory_items WHERE id = :i"), {"i": gone})).one()
        )
    async with user_scope(sessionmaker, p.uid) as s:
        for sql in (
            "UPDATE memory_items SET text_enc = 'x', fingerprint = 'y' WHERE id = :i",
            "UPDATE memory_items SET confidence = 0.1, type = 'date' WHERE id = :i",
            "DELETE FROM memory_items WHERE id = :i",
        ):
            assert (await s.execute(text(sql + " RETURNING id"), {"i": gone})).all() == [], sql
        assert (
            await s.execute(
                text("DELETE FROM memory_items WHERE id = :i RETURNING id"), {"i": live}
            )
        ).all() != []
    async with engine.connect() as c:
        after = tuple(
            (await c.execute(text("SELECT * FROM memory_items WHERE id = :i"), {"i": gone})).one()
        )
    assert after == before
    assert not await delete_mem(sessionmaker, p, gone)  # повтор — false


async def test_delete_function_clears_content_keeps_fingerprint(
    engine: AsyncEngine, sessionmaker: SM
) -> None:
    """АР-36: `app_delete_memory` стирает текст, вектор, модель, дату события; отпечаток, вид,
    источник, уверенность остаются; чужая/несуществующая — false; без входа/удалённый — 42501."""
    emb = WordsEmbedder()
    async with engine.begin() as c:
        p = await person(c, "Удаляю")
        q = await person(c, "Чужой")
        item = await add_mem(c, p, "любит горы")
        await c.execute(text("UPDATE memory_items SET event_date = '2026-01-02' WHERE id = :i"),
                        {"i": item})  # fmt: skip
    assert await chat_repo.set_memory_embedding(
        sessionmaker, p.uid, item, (await emb.embed(["любит горы"]))[0], emb.model
    )
    assert not await delete_mem(sessionmaker, q, item)
    assert not await delete_mem(sessionmaker, p, uuid.uuid4())
    assert await delete_mem(sessionmaker, p, item)
    async with engine.connect() as c:
        r = (
            await c.execute(
                text(
                    "SELECT text_enc, embedding, embedding_model, event_date, deleted_at, "
                    "fingerprint, type::text AS kind, confidence FROM memory_items WHERE id = :i"
                ),
                {"i": item},
            )
        ).one()
    assert (r.text_enc, r.embedding, r.embedding_model, r.event_date) == (None, None, None, None)
    assert r.deleted_at is not None and bytes(r.fingerprint) == my_fp(FPK, p.agent, "любит горы")
    assert r.kind == "fact"
    for uid in (None, uuid.uuid4()):
        with pytest.raises(DBAPIError) as e:
            async with user_scope(sessionmaker, uid) as s:
                await s.execute(text("SELECT app_delete_memory(:i)"), {"i": item})
        assert e.value.orig.sqlstate == "42501"  # type: ignore[union-attr]


# =============================================================== 3. Удалённое не возвращается


@pytest.fixture
async def forgot(engine: AsyncEngine, sessionmaker: SM) -> tuple[P, uuid.UUID]:
    """Владелец сказал «люблю горы», запись извлечена и удалена; дальше — новые сессии."""
    async with engine.begin() as c:
        p = await person(c, "Забыть")
        await say(c, p, "owner", "Я люблю горы, особенно зимой", ago(10))
    await release_others(engine, p)
    m = MemModel(items=["Любит горы"])
    out = await extract_for(engine, sessionmaker, p, m)
    assert out.status == "done" and out.inserted == 1
    [row] = await memory_of(engine, p)
    assert await delete_mem(sessionmaker, p, row.id)
    return p, row.id


async def test_same_session_never_recaptured(
    forgot: tuple[P, uuid.UUID], engine: AsyncEngine, sessionmaker: SM
) -> None:
    """§6.3 п.4, кр.1: обработанная сессия больше не захватывается (отметка), даже когда воркер
    гоняет проходы снова; модель её больше не видит."""
    p, _ = forgot
    m = MemModel(items=["Любит горы"])
    w = worker(engine, sessionmaker, m)
    for _ in range(3):
        await w.run_once()
    assert m.of("extract") == []
    assert await memory_of(engine, p) == []


@pytest.mark.parametrize(
    "variant",
    ["Любит горы", "ЛЮБИТ ГОРЫ!!!", "любит, горы.", "  любит\tгоры  ", "любит\u00a0горы"],
)
async def test_new_session_same_text_variants_caught(
    variant: str, forgot: tuple[P, uuid.UUID], engine: AsyncEngine, sessionmaker: SM
) -> None:
    """§6.3 п.4, кр.1, АР-36: новая сессия с тем же содержанием (регистр, пунктуация, пробелы,
    совместимые формы Unicode) — запись не возвращается (отпечаток), учтено `forgotten`."""
    p, _ = forgot
    async with engine.begin() as c:
        await say(c, p, "owner", "Опять про горы", ago(5000))
    out = await extract_for(engine, sessionmaker, p, MemModel(items=[variant]))
    assert (out.status, out.inserted, out.forgotten) == ("done", 0, 1)
    assert await memory_of(engine, p) == []


async def test_yo_variant_caught(engine: AsyncEngine, sessionmaker: SM) -> None:
    """«ё»/«е»: удалённое «ёлка дома» — повтор «елка дома» не возвращается."""
    async with engine.begin() as c:
        p = await person(c, "Ёлка")
        gone = await add_mem(c, p, "Ёлка дома до марта")
        await say(c, p, "owner", "про ёлку", ago(10))
    await release_others(engine, p)
    assert await delete_mem(sessionmaker, p, gone)
    out = await extract_for(engine, sessionmaker, p, MemModel(items=["елка дома до марта"]))
    assert out.forgotten == 1 and await memory_of(engine, p) == []


async def test_paraphrase_is_known_limitation(
    forgot: tuple[P, uuid.UUID], engine: AsyncEngine, sessionmaker: SM
) -> None:
    """Известное ограничение (АР-36, карточка): перефразировка другими словами отпечатком не
    ловится — запись появляется снова. Документирующий тест (не дефект по принятому решению)."""
    p, _ = forgot
    async with engine.begin() as c:
        await say(c, p, "owner", "Горы — моё всё", ago(5000))
    out = await extract_for(engine, sessionmaker, p, MemModel(items=["Обожает горы"]))
    assert out.inserted == 1
    assert [r.text for r in await memory_of(engine, p)] == ["Обожает горы"]


async def test_merge_never_restores_deleted(engine: AsyncEngine, sessionmaker: SM) -> None:
    """§6.3 п.1, п.4: (а) слияние, итог которого совпадает с удалённой записью, не пишется;
    (б) запись-получатель удалена владельцем во время слияния — итог слияния не пишется, кандидат
    сохраняется отдельно, удалённая остаётся удалённой."""
    emb = WordsEmbedder()
    async with engine.begin() as c:
        p = await person(c, "Слияние")
        old = await add_mem(c, p, "любит горы")
        target = await add_mem(c, p, "любит море летом в июле с детьми")
        await say(c, p, "owner", "про отдых", ago(10))
    await release_others(engine, p)
    for item, body in ((old, "любит горы"), (target, "любит море летом в июле с детьми")):
        assert await chat_repo.set_memory_embedding(
            sessionmaker, p.uid, item, (await emb.embed([body]))[0], emb.model
        )
    assert await delete_mem(sessionmaker, p, old)
    # (а) кандидат близок к живой записи, модель слияния даёт текст удалённой.
    m = MemModel(items=["любит море летом в июле с детьми вечером"], merge=(True, "Любит горы!"))
    out = await extract_for(engine, sessionmaker, p, m, emb=emb)
    assert out.status == "done" and m.of("merge")
    texts = [r.text for r in await memory_of(engine, p)]
    assert "Любит горы!" not in texts and all(my_norm(t) != "любит горы" for t in texts if t)
    # (б) получатель удаляется во время вызова модели слияния.
    async with engine.begin() as c:
        await say(c, p, "owner", "снова про отдых", ago(5000))
    live = {r.text: r.id for r in await memory_of(engine, p)}
    victim = live["любит море летом в июле с детьми"]

    async def delete_victim() -> None:
        assert await delete_mem(sessionmaker, p, victim)

    m2 = MemModel(items=["любит море летом в июле с детьми утром"], merge=(True, "МЕРЖ-ИТОГ"),
                  hooks={"merge": delete_victim})  # fmt: skip
    out2 = await extract_for(engine, sessionmaker, p, m2, emb=emb)
    assert out2.status == "done"
    rows = await memory_of(engine, p, live_only=False)
    assert all(r.text != "МЕРЖ-ИТОГ" for r in rows)
    assert next(r for r in rows if r.id == victim).deleted
    assert any(r.text == "любит море летом в июле с детьми утром" for r in rows)


async def test_deleted_during_extraction_not_reinserted(
    engine: AsyncEngine, sessionmaker: SM
) -> None:
    """Запись удалена, пока модель извлекает ту же запись из новой сессии, — отпечаток проверяется
    в транзакции записи: повтор не вставляется."""
    async with engine.begin() as c:
        p = await person(c, "Гонка")
        item = await add_mem(c, p, "боится собак")
        await say(c, p, "owner", "про собак", ago(10))
    await release_others(engine, p)

    async def delete_it() -> None:
        assert await delete_mem(sessionmaker, p, item)

    out = await extract_for(engine, sessionmaker, p,
                            MemModel(items=["Боится собак"], hooks={"extract": delete_it}))  # fmt: skip
    assert out.forgotten == 1 and out.inserted == 0 and out.repeated == 0
    assert await memory_of(engine, p) == []


async def test_old_key_still_catches_deleted(engine: AsyncEngine, sessionmaker: SM) -> None:
    """АР-36, ротация: запись удалена при старом ключе; после смены ключа (старый — в прежних)
    повтор ловится; без прежних ключей — не ловится (поэтому прежние ключи нельзя выбрасывать)."""
    old_key = os.urandom(32)
    async with engine.begin() as c:
        p = await person(c, "Ротация")
        item = await add_mem(c, p, "работает посменно", key=old_key)
        await say(c, p, "owner", "смены", ago(10))
    await release_others(engine, p)
    assert await delete_mem(sessionmaker, p, item)
    rotated = FingerprintKeys(os.urandom(32), (old_key,))
    out = await extract_for(engine, sessionmaker, p, MemModel(items=["Работает посменно"]),
                            fp=rotated)  # fmt: skip
    assert out.forgotten == 1 and await memory_of(engine, p) == []
    async with engine.begin() as c:
        await say(c, p, "owner", "снова смены", ago(5000))
    lost = FingerprintKeys(os.urandom(32))
    out2 = await extract_for(engine, sessionmaker, p, MemModel(items=["Работает посменно"]),
                             fp=lost)  # fmt: skip
    assert out2.inserted == 1  # без прежнего ключа удалённое не распознаётся


async def test_deleted_not_in_context_profile_or_dialog_summary(
    engine: AsyncEngine, sessionmaker: SM
) -> None:
    """Кр.1 и кр.4, АР-37 (2.4 р1, A3): удалённое — не в контексте 2.1 сразу после удаления;
    сжатие пересобирает сводку профиля только из живых (вход модели сводки — без удалённого),
    помеченную сводку диалога обнуляет; отметка сводки сохраняется."""
    async with engine.begin() as c:
        p = await person(c, "Сводки")
        live = await add_mem(c, p, "двое детей")
        gone = await add_mem(c, p, "Работает посменно")
        t1 = await say(c, p, "owner", "старое", ago(1))
        await say(c, p, "owner", "новое", ago(2))
        env = summary_envelope("Работает посменно. Любит чай, с сахаром.", ago(1), t1)
        await c.execute(text("UPDATE conversations SET summary_enc = :s WHERE id = :c"),
                        {"s": KEYS.seal(crypto.CONVERSATION_SUMMARY, p.conv, env), "c": p.conv})  # fmt: skip
        await c.execute(
            text("UPDATE agents SET profile_summary_enc = :s WHERE id = :a"),
            {
                "s": KEYS.seal(crypto.AGENT_PROFILE_SUMMARY, p.agent, "ПРОФИЛЬ: работает посменно"),
                "a": p.agent,
            },
        )
    # 2.4 р1 (A3), АР-37: удаление — путём API (`forget_memory`): профиль обнуляется, сводка
    # диалога помечена «на очистку» и в контекст не идёт уже сразу, до сжатия.
    assert await mem_ai.forget_memory(sessionmaker, KEYS, p.uid, gone) is not None
    ctx = await build_context(sessionmaker, KEYS, p.uid, p.conv, query="новое")
    assert gone not in ctx.stats.memory_ids and live in ctx.stats.memory_ids
    assert "посменно" not in json.dumps(ctx.payload(), ensure_ascii=False).lower()
    comp = [x for x in await mw.claim_compaction(async_sessionmaker(engine),
            every=timedelta(minutes=1), lease=timedelta(minutes=5), limit=100)
            if x.agent_id == p.agent]  # fmt: skip
    assert comp, "удаление владельцем должно вызывать сжатие (АР-31)"
    m = MemModel(profile="Двое детей.")
    out = await mem_ai.run_compaction(sessionmaker, KEYS, FP, models_of(m), comp[0])
    assert out.status == "done"
    [call] = m.of("profile")
    assert "посменно" not in json.dumps(call["payload"], ensure_ascii=False).lower()
    async with engine.connect() as c:
        r = (await c.execute(text("SELECT profile_summary_enc FROM agents WHERE id = :a"),
                             {"a": p.agent})).one()  # fmt: skip
        s = (await c.execute(text("SELECT summary_enc FROM conversations WHERE id = :c"),
                             {"c": p.conv})).one()  # fmt: skip
    assert KEYS.open(crypto.AGENT_PROFILE_SUMMARY, p.agent, r[0]) == "Двое детей."
    raw = json.loads(KEYS.open(crypto.CONVERSATION_SUMMARY, p.conv, s[0]))
    # 2.4 р1 (A3), АР-37: задача очистки не выполнялась («перезапуск») — сжатие обнуляет
    # помеченную сводку с сохранением отметки (приватность важнее непрерывности; прежнее
    # ожидание «Любит чай» осталось бы только при буквальном совпадении предложения).
    assert raw["text"] == "" and "forget" not in raw
    assert raw["through_id"] == str(t1)
    ctx2 = await build_context(sessionmaker, KEYS, p.uid, p.conv, query="новое")
    assert "посменно" not in json.dumps(ctx2.payload(), ensure_ascii=False).lower()


async def test_profile_keeps_deleted_until_next_compaction_period(
    engine: AsyncEngine, sessionmaker: SM
) -> None:
    """АР-37 (2.4 р1, A3; было — фиксация О-1 по АР-31): удаление сразу после сжатия обнуляет
    сводку профиля — удалённое не остаётся в контексте 2.1 до следующего сжатия."""
    async with engine.begin() as c:
        p = await person(c, "Период")
        item = await add_mem(c, p, "ТАЙНАa8 работает ночами")
        await say(c, p, "owner", "привет", ago(10))
    every = timedelta(days=7)
    comp = [x for x in await mw.claim_compaction(async_sessionmaker(engine), every=every,
            lease=timedelta(minutes=5), limit=100) if x.agent_id == p.agent]  # fmt: skip
    m = MemModel(profile="ТАЙНАa8 профиль")
    assert (
        await mem_ai.run_compaction(sessionmaker, KEYS, FP, models_of(m), comp[0])
    ).status == "done"
    # 2.4 р1 (A3), АР-37: удаление обнуляет сводку профиля сразу — удалённого в контексте нет и
    # до следующего сжатия (О-1 закрыт). Постановка на сжатие вне периода — функция БД (A1),
    # страж — `test_memory_worker.py::test_delete_queues_compaction_outside_period` (xfail).
    assert await mem_ai.forget_memory(sessionmaker, KEYS, p.uid, item) is not None
    ctx = await build_context(sessionmaker, KEYS, p.uid, p.conv, query="привет")
    assert "ТАЙНАa8" not in ctx.context.profile_summary
    # A8 р2 (строгая форма возвращена): с 0010 (АР-38) удаление ставит агента на сжатие вне
    # периода — следующий захват (период 7 дней не истёк) его выдаёт.
    again = [x for x in await mw.claim_compaction(async_sessionmaker(engine), every=every,
             lease=timedelta(minutes=5), limit=100) if x.agent_id == p.agent]  # fmt: skip
    assert len(again) == 1


async def test_compaction_rolls_back_when_record_deleted_midway(
    engine: AsyncEngine, sessionmaker: SM
) -> None:
    """Запись удалена во время сжатия — сжатие откатывается (`lost`), профиль не пишется; запись
    остаётся удалённой, следующее сжатие строит профиль без неё."""
    async with engine.begin() as c:
        p = await person(c, "Сжатие")
        a = await add_mem(c, p, "любит джаз")
        await add_mem(c, p, "любит джаз")  # дубликат
        await add_mem(c, p, "пьёт кофе")

    async def delete_a() -> None:
        assert await delete_mem(sessionmaker, p, a)

    every, lease = timedelta(minutes=1), timedelta(seconds=1)
    comp = [x for x in await mw.claim_compaction(async_sessionmaker(engine), every=every,
            lease=lease, limit=100) if x.agent_id == p.agent]  # fmt: skip
    m = MemModel(profile="ПРОФИЛЬ-С-ДЖАЗОМ", hooks={"profile": delete_a})
    out = await mem_ai.run_compaction(sessionmaker, KEYS, FP, models_of(m), comp[0])
    assert out.status == "lost"
    rows = await memory_of(engine, p, live_only=False)
    assert len(rows) == 3 and next(r for r in rows if r.id == a).deleted
    await asyncio.sleep(1.1)
    comp2 = [x for x in await mw.claim_compaction(async_sessionmaker(engine), every=every,
             lease=lease, limit=100) if x.agent_id == p.agent]  # fmt: skip
    m2 = MemModel(profile=lambda pl: " ".join(r["text"] for r in pl["records"]))
    out2 = await mem_ai.run_compaction(sessionmaker, KEYS, FP, models_of(m2), comp2[0])
    assert out2.status == "done"
    live = [r.text for r in await memory_of(engine, p)]
    # Живой дубликат удалённой записи — отдельная запись, видна владельцу в /memory (О-2).
    assert sorted(t or "" for t in live) == ["любит джаз", "пьёт кофе"]


# =============================================================== 4. Изоляция извлечения


MARKS_FOREIGN = {
    "partner_chat": "ЧАТИГОРЯa8",
    "partner_mem": "ПАМЯТЬИГОРЯa8",
    "partner_draft": "ЧЕРНОВИКИГОРЯa8",
    "partner_original": "ОРИГИНАЛИГОРЯa8",
    "partner_processing": "ЖДЁТИГОРЯa8",
    "partner_withheld": "ОСТАНОВЛЕНОИГОРЯa8",
    "own_sent": "СВОЁОТПРАВЛЕННОЕa8",
    "own_sent_adapted": "ДЛЯИГОРЯa8",
    "outsider": "ПОСТОРОННЯЯa8",
    "agent_reply": "ОТВЕТАГЕНТАa8",
}


async def test_extraction_isolation(engine: AsyncEngine, sessionmaker: SM) -> None:
    """Кр.2, ФТ-14, §9.2: во вход модели и в записи — только реплики владельца сессии и
    доставленные ему сообщения пары (версия, которую он видел). Не попадают: чат, память, черновики
    партнёра, оригинал адаптированного сообщения, `processing`/`withheld`, свои отправленные,
    ответы агента, посторонняя, id. Сообщение без версии для получателя (`adapted_enc` NULL) — не
    берётся (оригинал не читается). Распавшаяся пара: не берётся (АР-37, 2.4 р1 — A3;
    было «доставленное раньше — данные владельца»), недоставленное — нет."""
    async with engine.begin() as c:
        anna = await person(c, "Анна")
        igor = await person(c, "Игорь")
        ex = await person(c, "Павел")
        olga = await person(c, "Ольга")
        await say(c, anna, "owner", "Мне ВЛАДЕЛЕЦa8 тяжело", ago(10))
        await say(c, anna, "agent", MARKS_FOREIGN["agent_reply"], ago(11))
        await say(c, igor, "owner", MARKS_FOREIGN["partner_chat"], ago(10))
        await add_mem(c, igor, MARKS_FOREIGN["partner_mem"])
        await c.execute(
            text("INSERT INTO message_drafts (id, agent_id, text_enc) VALUES (:i, :a, :t)"),
            {
                "i": (d := uuid.uuid4()),
                "a": igor.agent,
                "t": KEYS.seal(crypto.DRAFT_TEXT, d, MARKS_FOREIGN["partner_draft"]),
            },
        )
        await say(c, olga, "owner", MARKS_FOREIGN["outsider"], ago(10))
        k, x = uuid.uuid4(), uuid.uuid4()
        await c.execute(text("INSERT INTO couples (id, status) VALUES (:k, 'active'), "
                             "(:x, 'dissolved')"), {"k": k, "x": x})  # fmt: skip
        await c.execute(
            text(
                "INSERT INTO couple_members (couple_id, user_id, role, active) VALUES "
                "(:x, :a, 'initiator', false), (:x, :e, 'partner', false), "
                "(:k, :a, 'initiator', true), (:k, :i, 'partner', true)"
            ),
            {"k": k, "x": x, "a": anna.uid, "i": igor.uid, "e": ex.uid},
        )
        OA, OI, OX = (anna.uid, anna.agent), (igor.uid, igor.agent), (ex.uid, ex.agent)
        delivered = await _pmsg(c, k, OI, anna.uid, MARKS_FOREIGN["partner_original"], "read",
                                ago(20), for_recipient="ВИДЕЛААННАa8", adapted=True)  # fmt: skip
        plain = await _pmsg(c, k, OI, anna.uid, "КАКЕСТЬa8", "delivered", ago(21),
                            for_recipient="КАКЕСТЬa8")  # fmt: skip
        no_version = await _pmsg(c, k, OI, anna.uid, "БЕЗВЕРСИИa8", "delivered", ago(22),
                                 for_recipient="tmp")  # fmt: skip
        await c.execute(text("UPDATE partner_messages SET adapted_enc = NULL WHERE id = :m"),
                        {"m": no_version})  # fmt: skip
        # Худший случай: у недоставленных есть версия для получателя (не должно быть, но статус —
        # единственный признак «владелец видел»).
        await _pmsg(c, k, OI, anna.uid, MARKS_FOREIGN["partner_processing"], "processing",
                    ago(23), for_recipient=MARKS_FOREIGN["partner_processing"])  # fmt: skip
        await _pmsg(c, k, OI, anna.uid, MARKS_FOREIGN["partner_withheld"], "withheld", ago(24),
                    for_recipient=MARKS_FOREIGN["partner_withheld"])  # fmt: skip
        await _pmsg(c, k, OA, igor.uid, MARKS_FOREIGN["own_sent"], "delivered", ago(25),
                    for_recipient=MARKS_FOREIGN["own_sent_adapted"], adapted=True)  # fmt: skip
        ex_msg = await _pmsg(c, x, OX, anna.uid, "ОТБЫВШЕГОa8", "delivered", ago(5),
                             for_recipient="ОТБЫВШЕГОa8")  # fmt: skip
        await _pmsg(c, x, OX, anna.uid, "БЫВШИЙЖДЁТa8", "processing", ago(6))
    await release_others(engine, anna)
    emb = WordsEmbedder()

    def answer(payload: dict[str, Any]) -> list[dict[str, Any]]:
        refs = [m["ref"] for m in payload["partner_messages"]]
        return [{"text": "владелец тяжело", "kind": "fact"}] + [
            {"text": f"партнёр {m['text']}", "kind": "from_partner", "partner_ref": m["ref"],
             "confidence": 0.8} for m in payload["partner_messages"]
        ] + [{"text": "наблюдение", "kind": "observation", "confidence": 0.99},
             {"text": "чужая ссылка", "kind": "from_partner", "partner_ref": "m999"},
             # 2.4 р1 (A3): реплики владельца и сообщения партнёра — разные вызовы (MINOR-2).
             *([{"text": "владелец со ссылкой", "kind": "fact", "partner_ref": refs[0]}]
               if refs else [])]  # fmt: skip

    m = MemModel(items=answer)
    out = await extract_for(engine, sessionmaker, anna, m, emb=emb)
    assert out.status == "done"
    sent = m.dump() + "\n".join(emb.inputs)
    assert not [k for k, v in MARKS_FOREIGN.items() if v in sent]
    assert not [i for i in (anna.uid, anna.agent, igor.uid, delivered) if str(i) in sent]
    assert "ВЛАДЕЛЕЦa8" in sent and "ВИДЕЛААННАa8" in sent and "КАКЕСТЬa8" in sent
    assert "БЕЗВЕРСИИa8" not in sent and "tmp" not in json.dumps(m.of("extract")[0]["payload"])
    # 2.4 р1 (A3), АР-37 (ФТ-14): только текущая активная пара — бывшая не берётся.
    assert "ОТБЫВШЕГОa8" not in sent and "БЫВШИЙЖДЁТa8" not in sent
    rows = await memory_of(engine, anna)
    by_text = {r.text: r for r in rows}
    assert by_text["владелец тяжело"].source == "owner_dialog"
    fp_rows = [r for r in rows if r.type == "from_partner"]
    # 2.4 р1 (A3), АР-37: сообщение бывшей пары (ex_msg) в память не идёт.
    assert {r.source_message_id for r in fp_rows} == {delivered, plain}
    assert ex_msg not in {r.source_message_id for r in rows}
    assert all(r.source == "partner_message" for r in fp_rows)
    obs = by_text["наблюдение"]
    assert (obs.type, obs.source, obs.confidence) == ("observation", "agent_inference", 0.6)
    assert "чужая ссылка" not in by_text
    # 2.4 р1 (A3), MINOR-2: в вызове по сообщениям партнёра источник задаёт код — «факт» из текста
    # партнёра записан как from_partner, а не как слова владельца.
    assert (by_text["владелец со ссылкой"].type, by_text["владелец со ссылкой"].source) == (
        "from_partner",
        "partner_message",
    )
    assert all(r.model == emb.model for r in rows)
    assert all(r.fingerprint == my_fp(FPK, anna.agent, r.text or "") for r in rows)
    # Партнёр: его память не тронута, записей Анны у него нет.
    assert [r.text for r in await memory_of(engine, igor)] == [MARKS_FOREIGN["partner_mem"]]


async def test_compaction_input_only_own(engine: AsyncEngine, sessionmaker: SM) -> None:
    """Вход модели сводки профиля — только свои живые записи (не партнёра, не удалённые)."""
    async with engine.begin() as c:
        anna = await person(c, "Анна2")
        igor = await person(c, "Игорь2")
        await add_mem(c, anna, "СВОЯa8")
        await add_mem(c, igor, "ЧУЖАЯa8")
    comp = [x for x in await mw.claim_compaction(async_sessionmaker(engine),
            every=timedelta(minutes=1), lease=timedelta(minutes=5), limit=100)
            if x.agent_id == anna.agent]  # fmt: skip
    m = MemModel()
    await mem_ai.run_compaction(sessionmaker, KEYS, FP, models_of(m), comp[0])
    assert "СВОЯa8" in m.dump() and "ЧУЖАЯa8" not in m.dump()


# =============================================================== 5. Воркер


@pytest.fixture
async def session_p(engine: AsyncEngine) -> P:
    async with engine.begin() as c:
        p = await person(c, "Воркер")
        await say(c, p, "owner", "Работаю посменно", ago(10))
    await release_others(engine, p)
    return p


async def test_two_copies_lease_and_takeover(
    session_p: P, engine: AsyncEngine, sessionmaker: SM
) -> None:
    """Кр.5, АР-36: вторая копия во время аренды работу не берёт; копия, «упавшая» посреди задачи,
    теряет её по истечении аренды; перехватившая доводит (attempts растёт); опоздавшая с чужим
    токеном — откат, двойной записи нет."""
    p = session_p
    lease = timedelta(seconds=1)
    first = await claim_for(engine, p, lease)
    assert first is not None and first.attempts == 1
    assert await claim_for(engine, p, lease) is None  # вторая копия — пусто
    await asyncio.sleep(1.2)  # первая «упала»
    second = await claim_for(engine, p, lease)
    assert second is not None and second.attempts == 2 and second.token != first.token
    # Опоздавшая первая копия завершает со старым токеном, пока вторая ещё держит аренду, — откат.
    out1 = await mem_ai.run_extraction(sessionmaker, KEYS, FP,
                                       models_of(MemModel(items=["ДВОЙНАЯa8"])), first)  # fmt: skip
    assert out1.status == "lost"
    assert await memory_of(engine, p) == []
    out2 = await mem_ai.run_extraction(sessionmaker, KEYS, FP, models_of(MemModel(items=["посменно"])),
                                       second)  # fmt: skip
    assert out2.status == "done" and out2.inserted == 1
    assert [r.text for r in await memory_of(engine, p)] == ["посменно"]
    assert await claim_for(engine, p) is None  # сессия обработана
    async with engine.connect() as c:
        att = (await c.execute(text("SELECT memory_attempts, memory_lease_token FROM agents "
                                    "WHERE id = :a"), {"a": p.agent})).one()  # fmt: skip
    assert tuple(att) == (0, None)


@pytest.mark.parametrize(
    ("err", "status", "moved"),
    [
        (LLMUnavailable("503"), "error", False),
        (LLMRefusal("no"), "refused", True),
        ({"bad": "shape"}, "error", False),
    ],
    ids=["unavailable", "refusal", "bad_output"],
)
async def test_model_failures(
    err: Any, status: str, moved: bool, session_p: P, engine: AsyncEngine, sessionmaker: SM
) -> None:
    """Кр.5: недоступность/испорченный ответ — `error`, отметка не движется, повтор после аренды;
    отказ — `refused`, сессия завершена без записей (повтора не будет)."""
    p = session_p
    m = MemModel(
        items=err if isinstance(err, BaseException) else (lambda _: [{"text": "x", "kind": "нет"}])
    )
    claim = await claim_for(engine, p, timedelta(seconds=1))
    out = await mem_ai.run_extraction(sessionmaker, KEYS, FP, models_of(m), claim)
    assert out.status == status and await memory_of(engine, p) == []
    await asyncio.sleep(1.2)
    again = await claim_for(engine, p, timedelta(seconds=1))
    assert (again is None) == moved


async def test_attempts_limit_finishes_without_extraction(
    session_p: P, engine: AsyncEngine, sessionmaker: SM
) -> None:
    """Кр.5: попыток больше предела — завершение без извлечения (модель не вызывается), сессия
    не зацикливает воркер."""
    p = session_p
    lease = timedelta(seconds=1)
    for _ in range(2):
        assert await claim_for(engine, p, lease) is not None
        await asyncio.sleep(1.1)
    claim = await claim_for(engine, p, lease)
    assert claim.attempts == 3
    m = MemModel(items=["не должно"])
    out = await mem_ai.run_extraction(sessionmaker, KEYS, FP, models_of(m), claim, max_attempts=2)
    assert out.status == "skipped" and m.calls == [] and await memory_of(engine, p) == []
    assert await claim_for(engine, p) is None


async def test_account_deleted_during_work(engine: AsyncEngine, sessionmaker: SM) -> None:
    """Кр.5, Б-44, АР-23: удаление аккаунта во время работы (между чтением и записью и
    одновременно с записью, 8 раз) — `gone` или `done` до удаления, без 40P01 и без следов
    (ни записей, ни агента)."""
    outcomes = []
    for i in range(8):
        async with engine.begin() as c:
            p = await person(c, f"Удаляемый{i}")
            await say(c, p, "owner", "что-то", ago(10))
        await release_others(engine, p)

        async def delete_account(p: P = p) -> None:
            async with user_scope(sessionmaker, p.uid) as s:
                await s.execute(text("SELECT app_delete_account()"))

        if i % 2 == 0:
            m = MemModel(items=["запись"], hooks={"extract": delete_account})
            outcomes.append((await extract_for(engine, sessionmaker, p, m)).status)
        else:
            claim = await claim_for(engine, p)
            m = MemModel(items=["запись"])
            job = asyncio.create_task(
                mem_ai.run_extraction(sessionmaker, KEYS, FP, models_of(m), claim)
            )
            await asyncio.sleep(0.005 * i)
            await delete_account()
            outcomes.append((await job).status)
        async with engine.connect() as c:
            left = (await c.execute(text("SELECT count(*) FROM memory_items WHERE agent_id = :a"),
                                    {"a": p.agent})).scalar_one()  # fmt: skip
            agents = (await c.execute(text("SELECT count(*) FROM agents WHERE id = :a"),
                                      {"a": p.agent})).scalar_one()  # fmt: skip
        assert (left, agents) == (0, 0)
    assert set(outcomes) <= {"gone", "done"} and "gone" in outcomes


async def test_run_once_full_pass_and_logs(
    engine: AsyncEngine, sessionmaker: SM, caplog: pytest.LogCaptureFixture
) -> None:
    """Кр.5: полный проход воркера — извлечение и сжатие; повторный проход пуст; журнал без
    текстов и id."""
    caplog.set_level(logging.DEBUG)
    async with engine.begin() as c:
        p = await person(c, "Проход")
        await say(c, p, "owner", "СЕКРЕТa8 работаю ночами", ago(10))
    await release_others(engine, p)
    m = MemModel(items=["СЕКРЕТa8 работает ночами"], profile="СЕКРЕТa8 профиль")
    w = worker(engine, sessionmaker, m, emb=WordsEmbedder())
    first = await w.run_once()
    kinds = {(o.kind, o.status) for o in first}
    assert ("extract", "done") in kinds and ("compact", "done") in kinds
    again = await w.run_once()
    assert [o for o in again if o.kind == "extract"] == []
    assert "СЕКРЕТa8" not in caplog.text and not UUID_RE.search(caplog.text)


# =============================================================== 6. /memory


@pytest.fixture
async def http(sessionmaker: SM) -> AsyncIterator[httpx.AsyncClient]:
    def settings() -> Settings:
        return Settings(
            telegram_bot_token=SecretStr("777004:A8-mem-bot-token-not-real"),
            session_secret=SecretStr("a8-mem-session-secret-" + "m" * 24),
            session_ttl_s=900,
            bot_username="A8_Mem_bot",
            env="test",
        )

    saved = (app.state.sessionmaker, app.state.keyring)
    app.state.sessionmaker, app.state.keyring = sessionmaker, KEYS
    app.dependency_overrides[get_settings] = settings
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://a8") as c:
        yield c
    app.dependency_overrides.clear()
    app.state.sessionmaker, app.state.keyring = saved


async def login(http: httpx.AsyncClient, label: str) -> tuple[uuid.UUID, dict[str, str]]:
    import time as _t

    from app.core.telegram import sign_init_data

    init = sign_init_data(
        {"auth_date": str(int(_t.time())),
         "user": json.dumps({"id": 760_000_000_000 + int.from_bytes(os.urandom(4), "big"),
                             "first_name": label})},
        "777004:A8-mem-bot-token-not-real",
    )  # fmt: skip
    r = await http.post("/api/v1/auth/telegram", json={"init_data": init})
    assert r.status_code == 200, r.text
    return uuid.UUID(r.json()["me"]["id"]), {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_memory_api(
    http: httpx.AsyncClient, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Кр.6, ФТ-13, контракт `/memory`: только свои живые записи, наблюдения помечены; без
    онбординга — 200 (без агента — пусто); `DELETE` — 204, повтор, чужая, несуществующая, неверный
    формат — 404 неразличимо; нерасшифрованная запись — `UnavailableText`, удаляется; удалённая —
    не в списке; `no-store`; журнал без текстов; токен удалённого — 401."""
    caplog.set_level(logging.DEBUG)
    a_uid, a_h = await login(http, "Анна")
    b_uid, _b_h = await login(http, "Игорь")
    assert (r := await http.get("/api/v1/memory", headers=a_h)).status_code == 200
    assert r.json() == {"items": []} and r.headers["cache-control"] == "no-store"
    async with engine.begin() as c:
        for uid in (a_uid, b_uid):
            await c.execute(text("INSERT INTO agents (id, user_id) VALUES (gen_random_uuid(), :u)"),
                            {"u": uid})  # fmt: skip
        ag = {u: (await c.execute(text("SELECT id FROM agents WHERE user_id = :u"), {"u": u}))
              .scalar_one() for u in (a_uid, b_uid)}  # fmt: skip
        pa = P(a_uid, ag[a_uid], uuid.uuid4())
        pb = P(b_uid, ag[b_uid], uuid.uuid4())
        fact = await add_mem(c, pa, "МОЁa8 любит горы")
        obs = await add_mem(c, pa, "НАБЛЮДЕНИЕa8", kind="observation", source="agent_inference")
        broken = await add_mem(c, pa, "СЛОМАНОa8")
        await c.execute(text("UPDATE memory_items SET text_enc = :b WHERE id = :i"),
                        {"b": b"\x00broken", "i": broken})  # fmt: skip
        foreign = await add_mem(c, pb, "ЧУЖОЕa8")
    items = (await http.get("/api/v1/memory", headers=a_h)).json()["items"]
    ids = {i["id"]: i for i in items}
    assert set(ids) == {str(fact), str(obs), str(broken)}
    assert (ids[str(obs)]["type"], ids[str(obs)]["source"]) == ("observation", "agent_inference")
    assert ids[str(broken)]["text"]["unavailable"] is True
    assert "ЧУЖОЕa8" not in json.dumps(items, ensure_ascii=False)
    only = (await http.get("/api/v1/memory", headers=a_h, params={"type": "observation"})).json()
    assert [i["id"] for i in only["items"]] == [str(obs)]
    assert (await http.get("/api/v1/memory", headers=a_h, params={"type": "x"})).status_code == 422
    statuses = []
    for target in (foreign, uuid.uuid4(), "not-a-uuid", fact, fact, broken):
        r = await http.delete(f"/api/v1/memory/{target}", headers=a_h)
        statuses.append(r.status_code)
        if r.status_code == 404:
            assert r.json()["code"] == "not_found"
        else:
            assert r.headers["cache-control"] == "no-store"
    assert statuses == [404, 404, 404, 204, 404, 204]
    left = (await http.get("/api/v1/memory", headers=a_h)).json()["items"]
    assert [i["id"] for i in left] == [str(obs)]
    assert [r.text for r in await memory_of(engine, pb)] == ["ЧУЖОЕa8"]
    assert (await http.get("/api/v1/memory")).status_code == 401
    server = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("httpx"))
    assert "МОЁa8" not in server and "СЛОМАНОa8" not in server
    assert not UUID_RE.search(server), server
    async with engine.begin() as c:
        await c.execute(text("DELETE FROM users WHERE id = :u"), {"u": a_uid})
    assert (await http.get("/api/v1/memory", headers=a_h)).status_code == 401
    assert (await http.delete(f"/api/v1/memory/{obs}", headers=a_h)).status_code == 401

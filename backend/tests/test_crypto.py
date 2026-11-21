"""4.2: шифрование при хранении, журналы без текстов (ТЗ §9.1 п.3, п.5; §7; §12 п.7).

Ключи — выдуманные, только для тестов. Сеть не нужна; БД — TEST_DATABASE_URL."""

import base64
import logging
import os
import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import ConfigError, Settings, get_settings
from app.core import crypto
from app.core.crypto import DecryptionError, Field, KeyConfigError, Keyring, parse_keyring
from app.repositories import texts
from tests.test_rls_isolation import World

ROW = uuid.UUID("00000000-0000-4000-8000-000000000001")
OTHER_ROW = uuid.UUID("00000000-0000-4000-8000-000000000002")
SECRET_TEXT = "мне страшно говорить об этом"


def _b64(key: bytes) -> str:
    return base64.urlsafe_b64encode(key).decode().rstrip("=")


K1 = os.urandom(32)
K2 = os.urandom(32)
RAW_K1 = f"k1:{_b64(K1)}"
RAW_K2 = f"k2:{_b64(K2)}"


def ring(raw: str = RAW_K1, current: str = "k1") -> Keyring:
    return parse_keyring(raw, current)


@pytest.fixture
def keys() -> Keyring:
    return ring()


# --- Формат и свойства AES-256-GCM -----------------------------------------------------------


def test_roundtrip_and_header(keys: Keyring) -> None:
    blob = keys.seal(crypto.MEMORY_TEXT, ROW, SECRET_TEXT)
    assert blob[0] == crypto.FORMAT_VERSION
    assert blob[1] == 2 and blob[2:4] == b"k1"
    assert crypto.key_id_of(blob) == "k1"
    # заголовок 4 + nonce 12 + текст + тег 16
    assert len(blob) == 4 + 12 + len(SECRET_TEXT.encode()) + 16
    assert SECRET_TEXT.encode() not in blob
    assert keys.open(crypto.MEMORY_TEXT, ROW, blob) == SECRET_TEXT


def test_empty_text_roundtrip(keys: Keyring) -> None:
    assert keys.open(crypto.DRAFT_TEXT, ROW, keys.seal(crypto.DRAFT_TEXT, ROW, "")) == ""


def test_nonce_is_random_96_bits_per_record(keys: Keyring) -> None:
    blobs = [keys.seal(crypto.MEMORY_TEXT, ROW, SECRET_TEXT) for _ in range(50)]
    nonces = {b[4:16] for b in blobs}
    assert len(nonces) == 50
    assert all(len(n) == 12 for n in nonces)
    assert len(set(blobs)) == 50


def test_every_flipped_byte_is_rejected(keys: Keyring) -> None:
    blob = keys.seal(crypto.MEMORY_TEXT, ROW, "привет")
    for i in range(len(blob)):
        for mask in (0x01, 0x80):
            tampered = bytearray(blob)
            tampered[i] ^= mask
            with pytest.raises(DecryptionError):
                keys.open(crypto.MEMORY_TEXT, ROW, bytes(tampered))


@pytest.mark.parametrize(
    "blob_fn",
    [
        lambda b: b"",
        lambda b: b[:1],
        lambda b: b[:10],
        lambda b: b[:-1],
        lambda b: b + b"\x00",
        lambda b: b"\x02" + b[1:],
        lambda b: b"\x01\x00" + b[4:],
        lambda b: b"\x01\x02\xff\xfe" + b[4:],
    ],
)
def test_malformed_ciphertext_is_rejected(keys: Keyring, blob_fn: Callable[[bytes], bytes]) -> None:
    blob = keys.seal(crypto.MEMORY_TEXT, ROW, SECRET_TEXT)
    with pytest.raises(DecryptionError):
        keys.open(crypto.MEMORY_TEXT, ROW, blob_fn(blob))


@pytest.mark.parametrize(
    ("field", "row"),
    [
        (crypto.MEMORY_TEXT, OTHER_ROW),  # другая строка
        (crypto.CONVERSATION_MESSAGE_TEXT, ROW),  # другая таблица
        (Field("memory_items", "other_enc"), ROW),  # другая колонка
    ],
)
def test_foreign_aad_is_rejected(keys: Keyring, field: Field, row: uuid.UUID) -> None:
    blob = keys.seal(crypto.MEMORY_TEXT, ROW, SECRET_TEXT)
    with pytest.raises(DecryptionError) as exc:
        keys.open(field, row, blob)
    assert exc.value.reason == "auth"


def test_foreign_key_with_same_id_is_rejected(keys: Keyring) -> None:
    blob = keys.seal(crypto.MEMORY_TEXT, ROW, SECRET_TEXT)
    other = ring(f"k1:{_b64(os.urandom(32))}")
    with pytest.raises(DecryptionError) as exc:
        other.open(crypto.MEMORY_TEXT, ROW, blob)
    assert exc.value.reason == "auth"


def test_unknown_key_id_is_rejected(keys: Keyring) -> None:
    blob = keys.seal(crypto.MEMORY_TEXT, ROW, SECRET_TEXT)
    with pytest.raises(DecryptionError) as exc:
        ring(RAW_K2, "k2").open(crypto.MEMORY_TEXT, ROW, blob)
    assert exc.value.reason == "unknown_key"


def test_key_id_in_header_is_authenticated() -> None:
    """Подмена id ключа в заголовке на другой ключ того же набора — отказ, не мусор."""
    both = ring(f"{RAW_K1},aa:{_b64(K1)}", "k1")
    blob = both.seal(crypto.MEMORY_TEXT, ROW, SECRET_TEXT)
    forged = blob[:2] + b"aa" + blob[4:]
    with pytest.raises(DecryptionError):
        both.open(crypto.MEMORY_TEXT, ROW, forged)


def test_non_utf8_plaintext_is_rejected(keys: Keyring) -> None:
    blob = keys.encrypt(b"\xff\xfe", crypto.MEMORY_TEXT.aad(ROW))
    with pytest.raises(DecryptionError) as exc:
        keys.open(crypto.MEMORY_TEXT, ROW, blob)
    assert exc.value.reason == "encoding"


def test_field_requires_uuid_and_identifiers() -> None:
    with pytest.raises(TypeError):
        crypto.MEMORY_TEXT.aad(str(ROW))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Field("memory_items|x", "text_enc")
    assert crypto.MEMORY_TEXT.aad(ROW) == f"memory_items|text_enc|{ROW}".encode()


def test_errors_and_repr_do_not_leak_key_or_text(keys: Keyring) -> None:
    blob = keys.seal(crypto.MEMORY_TEXT, ROW, SECRET_TEXT)
    for bad in (blob[:-1], blob[:2] + b"zz" + blob[4:]):
        with pytest.raises(DecryptionError) as exc:
            keys.open(crypto.MEMORY_TEXT, OTHER_ROW, bad)
        message = str(exc.value) + repr(exc.value)
        assert SECRET_TEXT not in message
        assert _b64(K1) not in message
        assert exc.value.__cause__ is None
    assert _b64(K1) not in repr(keys) and K1.hex() not in repr(keys)
    assert _b64(K1) not in repr(Settings(encryption_keys=RAW_K1, encryption_key_id="k1"))


# --- Ротация ---------------------------------------------------------------------------------


def test_rotation_reads_old_writes_current() -> None:
    old = ring(RAW_K1, "k1").seal(crypto.MEMORY_TEXT, ROW, SECRET_TEXT)
    rotated = ring(f"{RAW_K1},{RAW_K2}", "k2")
    assert rotated.current_key_id == "k2"
    assert rotated.key_ids == {"k1", "k2"}
    assert rotated.open(crypto.MEMORY_TEXT, ROW, old) == SECRET_TEXT
    new = rotated.seal(crypto.MEMORY_TEXT, ROW, SECRET_TEXT)
    assert crypto.key_id_of(new) == "k2"
    assert rotated.needs_rotation(old) and not rotated.needs_rotation(new)


def test_reencrypt_moves_record_to_current_key() -> None:
    old = ring(RAW_K1, "k1").seal(crypto.MEMORY_TEXT, ROW, SECRET_TEXT)
    rotated = ring(f"{RAW_K1},{RAW_K2}", "k2")
    moved = rotated.reseal(crypto.MEMORY_TEXT, ROW, old)
    assert crypto.key_id_of(moved) == "k2"
    # После перешифровки старый ключ можно убрать из набора.
    assert ring(RAW_K2, "k2").open(crypto.MEMORY_TEXT, ROW, moved) == SECRET_TEXT
    # Повторная перешифровка ничего не меняет; связанные данные сохраняются.
    assert rotated.reseal(crypto.MEMORY_TEXT, ROW, moved) == moved
    with pytest.raises(DecryptionError):
        rotated.open(crypto.MEMORY_TEXT, OTHER_ROW, moved)


def test_reencrypt_rejects_tampered_record() -> None:
    rotated = ring(f"{RAW_K1},{RAW_K2}", "k2")
    current = rotated.seal(crypto.MEMORY_TEXT, ROW, SECRET_TEXT)
    with pytest.raises(DecryptionError):
        rotated.reseal(crypto.MEMORY_TEXT, ROW, current[:-1] + bytes([current[-1] ^ 1]))
    with pytest.raises(DecryptionError):
        rotated.reseal(crypto.MEMORY_TEXT, OTHER_ROW, current)


# --- Набор ключей и запуск сервиса -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "current"),
    [
        ("", "k1"),
        (" , ", "k1"),
        (RAW_K1, ""),
        (RAW_K1, "k2"),
        (_b64(K1), "k1"),  # без id
        (f"k1:{_b64(K1[:16])}", "k1"),  # AES-128 — неверная длина
        (f"k1:{_b64(K1[:31])}", "k1"),
        (f"k1:{_b64(K1 + b'x')}", "k1"),
        (f"k1:{base64.b64encode(K1 + b'xx').decode()}", "k1"),  # 34 байта
        ("k1:not base64!", "k1"),
        ("k1:A", "k1"),  # битый base64
        (f"{RAW_K1},k1:{_b64(K2)}", "k1"),  # повтор id
        (f"k 1:{_b64(K1)}", "k 1"),  # недопустимый id
        (f"{'k' * 33}:{_b64(K1)}", "k" * 33),
    ],
)
def test_bad_keyring_is_rejected_without_leaking(raw: str, current: str) -> None:
    with pytest.raises(KeyConfigError) as exc:
        parse_keyring(raw, current)
    assert isinstance(exc.value, ConfigError)
    for secret in (_b64(K1), _b64(K2), _b64(K1[:16])):
        assert secret not in str(exc.value)
    assert exc.value.__cause__ is None


def test_keyring_accepts_padding_whitespace_and_several_keys() -> None:
    padded = base64.urlsafe_b64encode(K1).decode()
    assert padded.endswith("=")
    kr = parse_keyring(f" k1:{padded} , {RAW_K2} ", " k2 ")
    assert kr.key_ids == {"k1", "k2"} and kr.current_key_id == "k2"


def test_generate_key_entry_is_valid() -> None:
    entry = crypto.generate_key_entry("k7")
    kr = parse_keyring(entry, "k7")
    assert kr.open(crypto.MEMORY_TEXT, ROW, kr.seal(crypto.MEMORY_TEXT, ROW, "x")) == "x"
    with pytest.raises(ValueError):
        crypto.generate_key_entry("bad id")


def _start_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEELINGS_TELEGRAM_BOT_TOKEN", "123456:" + "T" * 35)
    monkeypatch.setenv("FEELINGS_SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("FEELINGS_BOT_USERNAME", "FeelingsTestBot")
    monkeypatch.setenv("FEELINGS_TELEGRAM_WEBHOOK_SECRET", "w" * 40)
    monkeypatch.setenv("FEELINGS_WEBAPP_URL", "https://app.example.test/")


@pytest.mark.parametrize(
    ("keys_env", "key_id_env"),
    [
        ("", "k1"),
        (RAW_K1, ""),
        (f"k1:{_b64(K1[:16])}", "k1"),
        (f"k1:{_b64(K1)}", "k9"),
    ],
)
async def test_service_does_not_start_without_valid_keys(
    monkeypatch: pytest.MonkeyPatch, keys_env: str, key_id_env: str
) -> None:
    from app.main import app, lifespan

    _start_env(monkeypatch)
    monkeypatch.setenv("FEELINGS_ENCRYPTION_KEYS", keys_env)
    monkeypatch.setenv("FEELINGS_ENCRYPTION_KEY_ID", key_id_env)
    saved = app.state.sessionmaker, app.state.bot_api, app.state.keyring
    app.state.sessionmaker = None
    get_settings.cache_clear()
    try:
        with pytest.raises(KeyConfigError) as exc:
            async with lifespan(app):
                pytest.fail("сервис запустился без ключей шифрования")
        assert app.state.sessionmaker is None
        assert _b64(K1) not in str(exc.value) and _b64(K1[:16]) not in str(exc.value)
    finally:
        get_settings.cache_clear()
        app.state.sessionmaker, app.state.bot_api, app.state.keyring = saved


async def test_service_starts_with_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import app, lifespan

    _start_env(monkeypatch)
    monkeypatch.setenv("FEELINGS_ENCRYPTION_KEYS", f"{RAW_K1},{RAW_K2}")
    monkeypatch.setenv("FEELINGS_ENCRYPTION_KEY_ID", "k2")
    saved = app.state.sessionmaker, app.state.bot_api, app.state.keyring
    app.state.sessionmaker = None
    get_settings.cache_clear()
    try:
        async with lifespan(app):
            assert isinstance(app.state.keyring, Keyring)
            assert app.state.keyring.current_key_id == "k2"
    finally:
        get_settings.cache_clear()
        app.state.sessionmaker, app.state.bot_api, app.state.keyring = saved


# --- Слой доступа и БД -----------------------------------------------------------------------


@pytest.fixture
async def encrypted_world(world: World, engine: AsyncEngine) -> World:
    """Фикстура world заводит тексты открытыми байтами (заглушка до 4.2, АР-8); слой доступа
    их честно отвергает, поэтому здесь они убираются — дальше только зашифрованные записи."""
    agents = [p.agent_id for p in (world.a, world.b, world.c)]
    convs = [p.conversation_id for p in (world.a, world.b, world.c)]
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM partner_messages WHERE couple_id = :c"), {"c": world.couple_id}
        )
        await conn.execute(
            text("DELETE FROM conversation_messages WHERE conversation_id = ANY(:c)"), {"c": convs}
        )
        await conn.execute(text("DELETE FROM memory_items WHERE agent_id = ANY(:a)"), {"a": agents})
        await conn.execute(
            text("DELETE FROM message_drafts WHERE agent_id = ANY(:a)"), {"a": agents}
        )
    return world


async def _plaintext_hits(engine: AsyncEngine, needle: str) -> list[str]:
    """Все bytea/text/varchar/jsonb колонки всех таблиц public, где встречается подстрока."""
    hits = []
    async with engine.connect() as conn:
        cols = (
            await conn.execute(
                text(
                    "SELECT c.table_name, c.column_name, c.data_type "
                    "FROM information_schema.columns c JOIN information_schema.tables t "
                    "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
                    "WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE' "
                    "AND c.data_type IN ('bytea', 'text', 'character varying', 'character', "
                    "'jsonb', 'json')"
                )
            )
        ).all()
        assert any(c.data_type == "bytea" for c in cols)
        for table, column, data_type in cols:
            if data_type == "bytea":
                cond = f'position(CAST(:n AS bytea) IN "{column}") > 0'
                param: object = needle.encode()
            else:
                cond = f'strpos(CAST("{column}" AS text), :n) > 0'
                param = needle
            found = (
                await conn.execute(
                    text(f'SELECT count(*) FROM "{table}" WHERE {cond}'), {"n": param}
                )
            ).scalar_one()
            if found:
                hits.append(f"{table}.{column}")
    return hits


async def _write_everything(
    w: World, sm: async_sessionmaker[AsyncSession], engine: AsyncEngine, kr: Keyring, tag: str
) -> dict[str, str]:
    """Каждый зашифрованный текст схемы — через слой доступа; возвращает открытые тексты."""
    t = {
        k: f"{k}-{tag}"
        for k in ("chat", "summary", "profile", "memory", "draft", "edited", "adapted")
    }
    a = w.a
    chat_id = await texts.add_conversation_message(
        sm, kr, a.user_id, a.conversation_id, "owner", t["chat"]
    )
    assert await texts.set_conversation_summary(sm, kr, a.user_id, a.conversation_id, t["summary"])
    assert await texts.set_agent_profile_summary(sm, kr, a.user_id, a.agent_id, t["profile"])
    mem_id = await texts.add_memory_item(
        sm, kr, a.user_id, a.agent_id, "fact", "owner_dialog", t["memory"]
    )
    draft = await texts.create_draft(sm, kr, a.user_id, a.agent_id, t["draft"])
    assert await texts.update_draft_text(sm, kr, a.user_id, draft, t["edited"]) == 2
    msg = await texts.send_draft(sm, a.user_id, draft, 2)
    async with engine.begin() as conn:
        s = AsyncSession(bind=conn)
        await s.execute(text("SET LOCAL ROLE feelings_worker"))
        assert await texts.pipeline_read_original(s, kr, msg, draft) == t["edited"]
        assert await texts.pipeline_deliver(
            s, kr, msg, t["adapted"], is_adapted=True, tone=1, harsh=False
        )
    # Чтение через слой доступа возвращает открытые тексты владельцу.
    chat = await texts.list_conversation_messages(sm, kr, a.user_id, a.conversation_id)
    assert [m.text for m in chat if m.id == chat_id] == [t["chat"]]
    assert (
        await texts.get_conversation_summary(sm, kr, a.user_id, a.conversation_id) == t["summary"]
    )
    assert await texts.get_agent_profile_summary(sm, kr, a.user_id, a.agent_id) == t["profile"]
    memory = await texts.list_memory_items(sm, kr, a.user_id, a.agent_id)
    assert [(m.id, m.text) for m in memory] == [(mem_id, t["memory"])]
    own_feed = {f.id: f for f in await texts.read_feed(sm, kr, a.user_id)}
    assert own_feed[msg].outgoing and own_feed[msg].text == t["edited"]
    partner_feed = {f.id: f for f in await texts.read_feed(sm, kr, w.b.user_id)}
    assert not partner_feed[msg].outgoing and partner_feed[msg].text == t["adapted"]
    assert partner_feed[msg].is_adapted
    assert await texts.read_original(sm, kr, w.b.user_id, msg) == t["edited"]
    assert await texts.read_original(sm, kr, w.c.user_id, msg) is None
    return t


async def test_no_plaintext_in_db_after_access_layer_writes(
    encrypted_world: World,
    sessionmaker: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    keys: Keyring,
) -> None:
    """Критерий RoadMap: после записи через слой доступа открытого текста в БД нет."""
    tag = uuid.uuid4().hex
    written = await _write_everything(encrypted_world, sessionmaker, engine, keys, tag)
    assert await _plaintext_hits(engine, tag) == []
    for value in written.values():
        assert await _plaintext_hits(engine, value) == []


async def test_plaintext_search_finds_unencrypted_text(world: World, engine: AsyncEngine) -> None:
    """Контроль поиска: незашифрованный текст (как в данных фикстуры) он находит."""
    tag = uuid.uuid4().hex
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO conversation_messages (conversation_id, author, text_enc) "
                "VALUES (:c, 'owner', :t)"
            ),
            {"c": world.c.conversation_id, "t": f"открыто {tag}".encode()},
        )
        await conn.execute(
            text("UPDATE users SET name = :n WHERE id = :u"), {"n": tag, "u": world.c.user_id}
        )
    assert sorted(await _plaintext_hits(engine, tag)) == [
        "conversation_messages.text_enc",
        "users.name",
    ]


async def test_no_plaintext_in_logs(
    encrypted_world: World,
    sessionmaker: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    keys: Keyring,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Критерий RoadMap: в журналах (включая SQL-журнал SQLAlchemy) открытых текстов нет."""
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.INFO, logger="sqlalchemy.engine")
    tag = uuid.uuid4().hex
    written = await _write_everything(encrypted_world, sessionmaker, engine, keys, tag)
    # Отказ RLS при записи в чужой диалог — тоже без текста в журнале и в исключении.
    with pytest.raises(DBAPIError) as exc:
        await texts.add_conversation_message(
            sessionmaker,
            keys,
            encrypted_world.c.user_id,
            encrypted_world.a.conversation_id,
            "owner",
            f"чужое {tag}",
        )
    assert tag not in str(exc.value)
    assert "INSERT INTO conversation_messages" in caplog.text  # журнал SQL действительно включён
    assert tag not in caplog.text
    for value in written.values():
        assert value not in caplog.text
    assert _b64(K1) not in caplog.text


async def test_access_layer_respects_owner(
    encrypted_world: World, sessionmaker: async_sessionmaker[AsyncSession], keys: Keyring
) -> None:
    """Негативные: чужой диалог, агент, черновик — не пишутся и не читаются."""
    a, c = encrypted_world.a, encrypted_world.c
    with pytest.raises(DBAPIError, match="row-level security"):
        await texts.add_conversation_message(
            sessionmaker, keys, c.user_id, a.conversation_id, "owner", "x"
        )
    with pytest.raises(DBAPIError, match="row-level security"):
        await texts.add_memory_item(
            sessionmaker, keys, c.user_id, a.agent_id, "fact", "owner_dialog", "x"
        )
    with pytest.raises(DBAPIError, match="row-level security"):
        await texts.create_draft(sessionmaker, keys, c.user_id, a.agent_id, "x")
    assert not await texts.set_conversation_summary(
        sessionmaker, keys, c.user_id, a.conversation_id, "x"
    )
    assert not await texts.set_agent_profile_summary(sessionmaker, keys, c.user_id, a.agent_id, "x")
    draft = await texts.create_draft(sessionmaker, keys, a.user_id, a.agent_id, "своё")
    assert await texts.get_draft(sessionmaker, keys, c.user_id, draft) is None
    assert await texts.update_draft_text(sessionmaker, keys, c.user_id, draft, "x") is None
    assert (
        await texts.list_conversation_messages(sessionmaker, keys, c.user_id, a.conversation_id)
        == []
    )
    assert await texts.list_memory_items(sessionmaker, keys, c.user_id, a.agent_id) == []
    assert await texts.get_agent_profile_summary(sessionmaker, keys, c.user_id, a.agent_id) is None
    got = await texts.get_draft(sessionmaker, keys, a.user_id, draft)
    assert got is not None and got.text == "своё" and got.version == 1


async def test_swapped_ciphertext_between_rows_is_rejected(
    encrypted_world: World,
    sessionmaker: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    keys: Keyring,
) -> None:
    """Поддельные данные: шифротекст, переставленный в другую строку, не расшифровывается."""
    a = encrypted_world.a
    first = await texts.add_memory_item(
        sessionmaker, keys, a.user_id, a.agent_id, "fact", "owner_dialog", "первое"
    )
    second = await texts.add_memory_item(
        sessionmaker, keys, a.user_id, a.agent_id, "fact", "owner_dialog", "второе"
    )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE memory_items SET text_enc = "
                "(SELECT text_enc FROM memory_items WHERE id = :f) WHERE id = :s"
            ),
            {"f": first, "s": second},
        )
    with pytest.raises(DecryptionError):
        await texts.list_memory_items(sessionmaker, keys, a.user_id, a.agent_id)


async def test_db_functions_pass_ciphertext_as_opaque_bytes(
    encrypted_world: World,
    sessionmaker: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    keys: Keyring,
) -> None:
    """app_send_draft, app_couple_feed, app_read_original, pipeline_* не меняют шифротекст."""
    a, b = encrypted_world.a, encrypted_world.b
    draft = await texts.create_draft(sessionmaker, keys, a.user_id, a.agent_id, SECRET_TEXT)
    msg = await texts.send_draft(sessionmaker, a.user_id, draft, 1)
    async with engine.connect() as conn:
        draft_blob = (
            await conn.execute(
                text("SELECT text_enc FROM message_drafts WHERE id = :d"), {"d": draft}
            )
        ).scalar_one()
        row = (
            await conn.execute(
                text("SELECT original_enc, draft_id FROM partner_messages WHERE id = :m"),
                {"m": msg},
            )
        ).one()
    # Оригинал — тот же шифротекст байт в байт; контекст — черновик, не новая строка.
    assert bytes(row.original_enc) == bytes(draft_blob)
    assert row.draft_id == draft
    assert keys.open(crypto.MESSAGE_ORIGINAL, draft, row.original_enc) == SECRET_TEXT
    with pytest.raises(DecryptionError):
        keys.open(crypto.MESSAGE_ADAPTED, msg, row.original_enc)
    with pytest.raises(DecryptionError):
        keys.open(Field("partner_messages", "original_enc"), msg, row.original_enc)

    adapted_blob = keys.seal(crypto.MESSAGE_ADAPTED, msg, "мягче")
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL ROLE feelings_worker"))
        original = (
            await conn.execute(text("SELECT pipeline_original(:m)"), {"m": msg})
        ).scalar_one()
        assert bytes(original) == bytes(draft_blob)
        ok = await conn.execute(
            text("SELECT pipeline_deliver(:m, :t, true, 1::smallint, false)"),
            {"m": msg, "t": adapted_blob},
        )
        assert ok.scalar_one() is True
    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                text("SELECT adapted_enc FROM partner_messages WHERE id = :m"), {"m": msg}
            )
        ).scalar_one()
    assert bytes(stored) == adapted_blob
    async with sessionmaker() as s, s.begin():
        await s.execute(text("SET LOCAL ROLE feelings_app"))
        await s.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(b.user_id)})
        feed = {
            r.id: bytes(r.text_enc)
            for r in await s.execute(text("SELECT id, text_enc FROM app_couple_feed(50, NULL)"))
        }
        assert feed[msg] == adapted_blob
        orig = (await s.execute(text("SELECT app_read_original(:m)"), {"m": msg})).scalar_one()
        assert bytes(orig) == bytes(draft_blob)

    # Остановленное безопасностью сообщение: pipeline_withhold не трогает шифротекст.
    draft2 = await texts.create_draft(sessionmaker, keys, a.user_id, a.agent_id, "второе")
    msg2 = await texts.send_draft(sessionmaker, a.user_id, draft2, 1)
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL ROLE feelings_worker"))
        assert (await conn.execute(text("SELECT pipeline_withhold(:m)"), {"m": msg2})).scalar_one()
    async with engine.connect() as conn:
        kept = (
            await conn.execute(
                text("SELECT original_enc FROM partner_messages WHERE id = :m"), {"m": msg2}
            )
        ).scalar_one()
    assert keys.open(crypto.MESSAGE_ORIGINAL, draft2, kept) == "второе"


async def test_original_of_one_message_does_not_open_in_another(
    encrypted_world: World,
    sessionmaker: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    keys: Keyring,
) -> None:
    """Контекст черновика сохраняет защиту от перестановки и для сообщений пары."""
    a = encrypted_world.a
    d1 = await texts.create_draft(sessionmaker, keys, a.user_id, a.agent_id, "одно")
    m1 = await texts.send_draft(sessionmaker, a.user_id, d1, 1)
    d2 = await texts.create_draft(sessionmaker, keys, a.user_id, a.agent_id, "другое")
    m2 = await texts.send_draft(sessionmaker, a.user_id, d2, 1)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE partner_messages SET original_enc = "
                "(SELECT original_enc FROM partner_messages WHERE id = :x) WHERE id = :y"
            ),
            {"x": m1, "y": m2},
        )
    assert await texts.read_original(sessionmaker, keys, a.user_id, m1) == "одно"
    with pytest.raises(DecryptionError):
        await texts.read_original(sessionmaker, keys, a.user_id, m2)
    async with engine.begin() as conn:
        s = AsyncSession(bind=conn)
        await s.execute(text("SET LOCAL ROLE feelings_worker"))
        with pytest.raises(DecryptionError):
            await texts.pipeline_read_original(s, keys, m1, d2)  # чужой черновик в задании


async def test_rotation_through_access_layer(
    encrypted_world: World, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Записанное под старым ключом читается после смены текущего ключа."""
    a = encrypted_world.a
    old = ring(RAW_K1, "k1")
    await texts.add_conversation_message(
        sessionmaker, old, a.user_id, a.conversation_id, "agent", "до"
    )
    new = ring(f"{RAW_K1},{RAW_K2}", "k2")
    await texts.add_conversation_message(
        sessionmaker, new, a.user_id, a.conversation_id, "agent", "после"
    )
    got = [
        m.text
        for m in await texts.list_conversation_messages(
            sessionmaker, new, a.user_id, a.conversation_id
        )
        if m.author == "agent"
    ]
    assert got == ["до", "после"]

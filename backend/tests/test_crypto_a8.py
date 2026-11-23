"""A8, задача 4.2: независимые тесты шифрования при хранении (ТЗ §9.1 п.3, п.5; §7; §12 п.7;
справка по инфраструктуре §5 — ключ отдельно от базы).

Не используют помощники исполнителя (`tests/test_crypto.py`): формат проверяется расшифровкой
библиотекой `cryptography` напрямую по описанию в карточке, данные заводятся своей фикстурой,
поиск открытого текста в БД — свой перебор всех колонок без списка типов.

Ключи и тексты — выдуманные, только для тестов. Сеть не нужна; БД — TEST_DATABASE_URL."""

import base64
import logging
import traceback
import uuid
from collections.abc import Iterator

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import ConfigError, get_settings
from app.core import crypto
from app.core.crypto import DecryptionError, KeyConfigError, Keyring, parse_keyring
from app.repositories import texts

# 2.4 (A3): отпечаток записи памяти обязателен (Б-68) — выдуманный ключ тестов.
from tests.conftest import TEST_FP
from tests.test_rls_isolation import World

SM = async_sessionmaker[AsyncSession]

# Выдуманные ключи: фиксированные байты (не случайные), чтобы известный вектор был воспроизводим.
KA = bytes(range(32))
KB = bytes(range(32, 64))
KC = bytes(range(64, 96))


def b64(key: bytes, pad: bool = False) -> str:
    s = base64.urlsafe_b64encode(key).decode()
    return s if pad else s.rstrip("=")


def kr(spec: dict[str, bytes], current: str) -> Keyring:
    return parse_keyring(",".join(f"{k}:{b64(v)}" for k, v in spec.items()), current)


def header(kid: str) -> bytes:
    """Заголовок по описанию карточки: 0x01 | len(key_id) | key_id."""
    return bytes((1, len(kid))) + kid.encode("ascii")


def context(table: str, column: str, row: uuid.UUID) -> bytes:
    return f"{table}|{column}|{row}".encode("ascii")


ROW = uuid.UUID("11111111-2222-4333-8444-555555555555")
ROW2 = uuid.UUID("11111111-2222-4333-8444-666666666666")
MEMORY_CTX = context("memory_items", "text_enc", ROW)


def leaks(exc: BaseException, *secrets: str | bytes) -> list[str]:
    """Что из секретов видно в str/repr/трассировке исключения (включая цепочку)."""
    shown = str(exc) + repr(exc) + "".join(traceback.format_exception(exc))
    chain = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    if chain is not None:
        shown += str(chain) + repr(chain)
    found = []
    for s in secrets:
        if isinstance(s, bytes):
            if repr(s) in shown or s.hex() in shown:
                found.append(repr(s)[:20])
        elif s and s in shown:
            found.append(s[:20])
    return found


# --- 1. Формат: известные векторы (карточка «Реализация», формат v1) ------------------------


# Вектор посчитан библиотекой cryptography напрямую (AESGCM), без кода приложения:
# ключ KA, id «a8», nonce bytes(100..111), AAD = заголовок + «memory_items|text_enc|ROW».
KNOWN_VECTOR = bytes.fromhex(
    "010261386465666768696a6b6c6d6e6f98a90feda95d871deede8f580ad8ba409272d785abbc4ca2170110"
    "997472279918ab44ead712b2f70772cedffff8452559"
)
KNOWN_PLAIN = "выдуманная память"


def test_known_vector_is_opened_by_keyring() -> None:
    """Шифротекст, собранный вручную по описанию формата, приложение открывает."""
    ring = kr({"a8": KA}, "a8")
    assert ring.open(crypto.MEMORY_TEXT, ROW, KNOWN_VECTOR) == KNOWN_PLAIN
    manual = header("a8") + bytes(range(100, 112))
    manual += AESGCM(KA).encrypt(
        bytes(range(100, 112)), KNOWN_PLAIN.encode(), header("a8") + MEMORY_CTX
    )
    assert manual == KNOWN_VECTOR


@pytest.mark.parametrize("kid", ["a", "a8", "K_-9", "x" * 32])
def test_own_ciphertext_decrypts_with_library_by_described_format(kid: str) -> None:
    """Обратное направление: запись приложения разбирается по описанию и открывается AESGCM."""
    ring = kr({kid: KA}, kid)
    plain = "мне обидно, но я хочу поговорить"
    blob = ring.seal(crypto.MEMORY_TEXT, ROW, plain)
    assert blob[0] == 0x01
    assert blob[1] == len(kid)
    assert blob[2 : 2 + len(kid)] == kid.encode()
    nonce = blob[2 + len(kid) : 14 + len(kid)]
    sealed = blob[14 + len(kid) :]
    assert len(nonce) == 12
    assert len(sealed) == len(plain.encode()) + 16  # шифротекст + тег 16 байт
    assert AESGCM(KA).decrypt(nonce, sealed, header(kid) + MEMORY_CTX).decode() == plain
    # AAD действительно = заголовок + контекст: без заголовка или без контекста — отказ.
    for wrong in (MEMORY_CTX, header(kid), header(kid) + context("memory_items", "text_enc", ROW2)):
        with pytest.raises(InvalidTag):
            AESGCM(KA).decrypt(nonce, sealed, wrong)
    assert crypto.key_id_of(blob) == kid


@pytest.mark.parametrize(
    ("field", "table", "column"),
    [
        (crypto.DRAFT_TEXT, "message_drafts", "text_enc"),
        (crypto.MESSAGE_ORIGINAL, "message_drafts", "text_enc"),  # оригинал = контекст черновика
        (crypto.MESSAGE_ADAPTED, "partner_messages", "adapted_enc"),
        (crypto.CONVERSATION_MESSAGE_TEXT, "conversation_messages", "text_enc"),
        (crypto.MEMORY_TEXT, "memory_items", "text_enc"),
        (crypto.CONVERSATION_SUMMARY, "conversations", "summary_enc"),
        (crypto.AGENT_PROFILE_SUMMARY, "agents", "profile_summary_enc"),
    ],
)
def test_aad_of_every_column_matches_description(
    field: crypto.Field, table: str, column: str
) -> None:
    ring = kr({"a8": KA}, "a8")
    blob = ring.seal(field, ROW, "x")
    assert AESGCM(KA).decrypt(blob[4:16], blob[16:], header("a8") + context(table, column, ROW))


def test_nonce_unique_over_10000_encryptions() -> None:
    """Карточка: случайный nonce 96 бит на запись — 10 000 записей одного текста, повторов нет."""
    ring = kr({"a8": KA}, "a8")
    blobs = [ring.seal(crypto.MEMORY_TEXT, ROW, "один и тот же текст") for _ in range(10_000)]
    nonces = {b[4:16] for b in blobs}
    assert len(nonces) == 10_000
    assert len({b[16:] for b in blobs}) == 10_000
    assert all(len(b) == len(blobs[0]) for b in blobs)


# --- 2. Подмена каждого поля заголовка и тела -----------------------------------------------


def _ring_with_aliases() -> Keyring:
    # Тот же ключ под несколькими id: подмена id в заголовке на «рабочий» id ловится только
    # потому, что заголовок входит в AAD.
    return kr({"a8": KA, "b8": KA, "a": KA, "zz": KB}, "a8")


def _tampered() -> list[tuple[str, object, set[str]]]:
    def setb(i: int, v: int) -> object:
        return lambda b: b[:i] + bytes([v]) + b[i + 1 :]

    def flip(i: int) -> object:
        return lambda b: b[:i] + bytes([b[i] ^ 0x01]) + b[i + 1 :]

    return [
        ("version=0", setb(0, 0x00), {"format"}),
        ("version=2", setb(0, 0x02), {"format"}),
        ("version=255", setb(0, 0xFF), {"format"}),
        ("kid_len=0", setb(1, 0), {"format"}),
        ("kid_len=1 (id «a» есть в наборе)", setb(1, 1), {"auth"}),
        ("kid_len=3", setb(1, 3), {"format", "unknown_key"}),
        ("kid_len=255", setb(1, 255), {"format"}),
        ("kid → b8 (тот же ключ)", setb(2, ord("b")), {"auth"}),
        ("kid → zz-подобный неизвестный", setb(2, ord("q")), {"unknown_key"}),
        ("kid не ASCII", setb(2, 0xC3), {"format"}),
        ("kid недопустимый символ", setb(2, ord("|")), {"format"}),
        ("nonce[0]", flip(4), {"auth"}),
        ("nonce[11]", flip(15), {"auth"}),
        ("шифротекст[0]", flip(16), {"auth"}),
        ("тег, последний байт", lambda b: b[:-1] + bytes([b[-1] ^ 1]), {"auth"}),
        ("обрезан тег", lambda b: b[:-1], {"auth", "format"}),
        ("только заголовок и nonce", lambda b: b[:16], {"format"}),
        ("лишний байт в конце", lambda b: b + b"\x00", {"auth"}),
        ("пусто", lambda b: b"", {"format"}),
    ]


@pytest.mark.parametrize(("name", "mutate", "reasons"), _tampered(), ids=lambda v: str(v)[:30])
def test_tampered_header_or_body_is_rejected(name: str, mutate: object, reasons: set[str]) -> None:
    ring = _ring_with_aliases()
    plain = "секрет подмены"
    blob = ring.seal(crypto.MEMORY_TEXT, ROW, plain)
    assert callable(mutate)
    bad = mutate(blob)
    assert bad != blob
    with pytest.raises(DecryptionError) as exc:
        ring.open(crypto.MEMORY_TEXT, ROW, bad)
    assert exc.value.reason in reasons, name
    assert not leaks(exc.value, plain, KA, b64(KA))


def test_foreign_key_and_foreign_context_are_errors_not_garbage() -> None:
    blob = kr({"a8": KA}, "a8").seal(crypto.MEMORY_TEXT, ROW, "моё")
    with pytest.raises(DecryptionError) as e1:
        kr({"a8": KB}, "a8").open(crypto.MEMORY_TEXT, ROW, blob)
    assert e1.value.reason == "auth"
    for field, row in (
        (crypto.MEMORY_TEXT, ROW2),
        (crypto.CONVERSATION_MESSAGE_TEXT, ROW),
        (crypto.DRAFT_TEXT, ROW),
    ):
        with pytest.raises(DecryptionError) as e2:
            kr({"a8": KA}, "a8").open(field, row, blob)
        assert e2.value.reason == "auth"


# --- 3. Набор ключей из окружения -------------------------------------------------------------


def test_keys_with_whitespace_padding_and_several_entries() -> None:
    raw = f"  a8 : {b64(KA, pad=True)} ,, b8:{b64(KB)} ,  c8 :{b64(KC, pad=True)}  ,"
    ring = parse_keyring(raw, "  b8 ")
    assert ring.current_key_id == "b8"
    assert ring.key_ids == {"a8", "b8", "c8"}
    # Ключ с паддингом и без — один и тот же ключ.
    padded = parse_keyring(f"a8:{b64(KA, pad=True)}", "a8")
    bare = parse_keyring(f"a8:{b64(KA)}", "a8")
    assert bare.open(crypto.MEMORY_TEXT, ROW, padded.seal(crypto.MEMORY_TEXT, ROW, "x")) == "x"
    assert ring.open(crypto.MEMORY_TEXT, ROW, KNOWN_VECTOR) == KNOWN_PLAIN


GOOD = b64(KA)
BAD_KEYRINGS = [
    ("", "a8"),
    ("   ", "a8"),
    (",,, ,", "a8"),
    (f"a8:{GOOD},a8:{b64(KB)}", "a8"),  # дубль id
    (f"a8:{GOOD}, a8 :{GOOD}", "a8"),  # дубль id с пробелами
    (f"a 8:{GOOD}", "a 8"),
    (f"a|8:{GOOD}", "a|8"),
    (f"к8:{GOOD}", "к8"),  # кириллица в id
    (f"{'x' * 33}:{GOOD}", "x" * 33),
    (f":{GOOD}", ""),
    (GOOD, "a8"),  # нет id — значение ключа на месте записи
    ("a8:", "a8"),
    ("a8: ", "a8"),
    (f"a8:{GOOD[:20]} {GOOD[20:]}", "a8"),  # пробел внутри значения
    (f"a8:{base64.b64encode(bytes([0xFB] * 32)).decode()}", "a8"),  # base64 с «+/», не url
    (f"a8:{GOOD}===", "a8"),
    (f"a8:{b64(KA[:16])}", "a8"),
    (f"a8:{b64(KA[:31])}", "a8"),
    (f"a8:{b64(KA + b'x')}", "a8"),
    (f"a8:{b64(KA + KB)}", "a8"),
    (f"a8:{GOOD}", "b8"),  # текущий не из набора
    (f"a8:{GOOD}", ""),
    (f"a8:{GOOD}", "   "),
    (f"a8:{GOOD},b8:{b64(KB)}", "a8,b8"),
]


@pytest.mark.parametrize(("raw", "current"), BAD_KEYRINGS)
def test_bad_keyring_rejected_without_leaking_values(raw: str, current: str) -> None:
    with pytest.raises(KeyConfigError) as exc:
        parse_keyring(raw, current)
    assert isinstance(exc.value, ConfigError)
    # Ни значение ключа, ни его заметная часть не видны нигде в исключении.
    secrets = [GOOD, GOOD[:20], GOOD[20:], b64(KB), b64(KA[:16]), b64(KA[:31]), KA]
    assert not leaks(exc.value, *secrets)


def test_keyring_direct_construction_checks() -> None:
    with pytest.raises(KeyConfigError):
        Keyring({}, "a8")
    with pytest.raises(KeyConfigError):
        Keyring({"a8": KA[:31]}, "a8")
    with pytest.raises(KeyConfigError):
        Keyring({"a 8": KA}, "a 8")
    with pytest.raises(KeyConfigError):
        Keyring({"a8": KA}, "b8")


def test_keyring_repr_and_settings_repr_have_no_key_values(monkeypatch: pytest.MonkeyPatch) -> None:
    ring = kr({"a8": KA, "b8": KB}, "b8")
    shown = repr(ring) + str(ring)
    assert b64(KA) not in shown and b64(KB) not in shown and KA.hex() not in shown
    assert not hasattr(ring, "__dict__")
    monkeypatch.setenv("FEELINGS_ENCRYPTION_KEYS", f"a8:{b64(KA)}")
    get_settings.cache_clear()
    try:
        s = get_settings()
        assert b64(KA) not in repr(s) and b64(KA) not in str(s.model_dump())
    finally:
        get_settings.cache_clear()


def test_decryption_error_has_no_plaintext_or_key() -> None:
    ring = kr({"a8": KA}, "a8")
    plain = "очень личное признание"
    blob = ring.seal(crypto.MEMORY_TEXT, ROW, plain)
    for bad_row, bad_blob in ((ROW2, blob), (ROW, blob[:-1] + b"\x00")):
        with pytest.raises(DecryptionError) as exc:
            ring.open(crypto.MEMORY_TEXT, bad_row, bad_blob)
        assert not leaks(exc.value, plain, KA, b64(KA), str(bad_row))
        assert exc.value.__cause__ is None and exc.value.__suppress_context__
    non_utf8 = ring.encrypt(b"\xff\xfe", MEMORY_CTX)
    with pytest.raises(DecryptionError) as exc:
        ring.open(crypto.MEMORY_TEXT, ROW, non_utf8)
    assert exc.value.reason == "encoding" and exc.value.__suppress_context__


# --- 4. Ротация на трёх ключах ---------------------------------------------------------------


def test_rotation_over_three_keys() -> None:
    plain = {"a8": "под первым", "b8": "под вторым", "c8": "под третьим"}
    rows = {"a8": ROW, "b8": ROW2, "c8": uuid.UUID(int=7)}
    stages = [
        ({"a8": KA}, "a8"),
        ({"a8": KA, "b8": KB}, "b8"),
        ({"a8": KA, "b8": KB, "c8": KC}, "c8"),
    ]
    blobs = {}
    for spec, current in stages:
        ring = kr(spec, current)
        blobs[current] = ring.seal(crypto.MEMORY_TEXT, rows[current], plain[current])
        assert crypto.key_id_of(blobs[current]) == current  # новые записи — текущим ключом
        for kid, blob in blobs.items():  # старые читаются
            assert ring.open(crypto.MEMORY_TEXT, rows[kid], blob) == plain[kid]
    full = kr(stages[-1][0], "c8")
    assert [full.needs_rotation(blobs[k]) for k in ("a8", "b8", "c8")] == [True, True, False]
    moved = {k: full.reseal(crypto.MEMORY_TEXT, rows[k], b) for k, b in blobs.items()}
    assert moved["c8"] == blobs["c8"]  # уже под текущим — как есть
    assert {crypto.key_id_of(b) for b in moved.values()} == {"c8"}
    # Старые ключи выведены из набора: перешифрованное читается, неперешифрованное — нет.
    only_c = kr({"c8": KC}, "c8")
    for k, b in moved.items():
        assert only_c.open(crypto.MEMORY_TEXT, rows[k], b) == plain[k]
        with pytest.raises(DecryptionError):  # перешифровка сохраняет привязку к строке
            only_c.open(crypto.MEMORY_TEXT, uuid.UUID(int=99), b)
    with pytest.raises(DecryptionError) as exc:
        only_c.open(crypto.MEMORY_TEXT, rows["a8"], blobs["a8"])
    assert exc.value.reason == "unknown_key"
    # Перешифровка не «узаконивает» подделку ни под старым, ни под текущим ключом.
    for k in ("a8", "c8"):
        with pytest.raises(DecryptionError):
            full.reseal(crypto.MEMORY_TEXT, rows[k], blobs[k][:-1] + bytes([blobs[k][-1] ^ 1]))
        with pytest.raises(DecryptionError):
            full.reseal(crypto.MEMORY_TEXT, uuid.UUID(int=99), blobs[k])


# --- 5. Сервис не стартует без ключей (lifespan) ---------------------------------------------


def _service_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEELINGS_TELEGRAM_BOT_TOKEN", "123456:" + "Q" * 35)
    monkeypatch.setenv("FEELINGS_SESSION_SECRET", "z" * 40)
    monkeypatch.setenv("FEELINGS_BOT_USERNAME", "FeelingsA8Bot")
    monkeypatch.setenv("FEELINGS_TELEGRAM_WEBHOOK_SECRET", "h" * 40)
    monkeypatch.setenv("FEELINGS_WEBAPP_URL", "https://a8.example.test/")


@pytest.fixture
def isolated_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    import app.main as main

    _service_env(monkeypatch)
    saved = main.app.state.sessionmaker, main.app.state.bot_api, main.app.state.keyring
    main.app.state.sessionmaker = None
    main.app.state.keyring = None
    get_settings.cache_clear()
    try:
        yield main
    finally:
        get_settings.cache_clear()
        main.app.state.sessionmaker, main.app.state.bot_api, main.app.state.keyring = saved


@pytest.mark.parametrize(
    ("keys_env", "kid_env"),
    [
        (None, None),  # переменных нет вовсе
        (None, "a8"),
        (f"a8:{GOOD}", None),
        ("", ""),
        (f"a8:{b64(KA[:16])}", "a8"),  # AES-128 — не AES-256
        (f"a8:{b64(KA + b'x')}", "a8"),
        (f"a8:{GOOD}", "b8"),
        (f"a8:{GOOD},a8:{GOOD}", "a8"),
    ],
)
async def test_service_does_not_start_without_valid_keys(
    isolated_app: object,
    monkeypatch: pytest.MonkeyPatch,
    keys_env: str | None,
    kid_env: str | None,
) -> None:
    main = isolated_app
    for name, value in (
        ("FEELINGS_ENCRYPTION_KEYS", keys_env),
        ("FEELINGS_ENCRYPTION_KEY_ID", kid_env),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    def no_engine(*_a: object, **_k: object) -> None:
        pytest.fail("движок БД создан до проверки ключей")

    monkeypatch.setattr(main, "make_engine", no_engine)  # type: ignore[attr-defined]
    monkeypatch.setattr(main, "BotApi", lambda *_a: pytest.fail("клиент бота создан"))  # type: ignore[attr-defined]
    with pytest.raises(KeyConfigError) as exc:
        async with main.lifespan(main.app):  # type: ignore[attr-defined]
            pytest.fail("сервис запустился без ключей шифрования")
    assert main.app.state.sessionmaker is None  # type: ignore[attr-defined]
    assert main.app.state.keyring is None  # type: ignore[attr-defined]
    assert not leaks(exc.value, GOOD, b64(KA[:16]), b64(KA + b"x"), KA)


async def test_service_starts_with_three_keys(
    isolated_app: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = isolated_app
    monkeypatch.setenv(
        "FEELINGS_ENCRYPTION_KEYS", f"a8:{b64(KA)}, b8:{b64(KB, pad=True)} ,c8:{b64(KC)}"
    )
    monkeypatch.setenv("FEELINGS_ENCRYPTION_KEY_ID", "b8")
    async with main.lifespan(main.app):  # type: ignore[attr-defined]
        ring = main.app.state.keyring  # type: ignore[attr-defined]
        assert isinstance(ring, Keyring)
        assert ring.current_key_id == "b8" and ring.key_ids == {"a8", "b8", "c8"}
        assert ring.open(crypto.MEMORY_TEXT, ROW, KNOWN_VECTOR) == KNOWN_PLAIN


# --- 6. Слой доступа texts.py и БД ----------------------------------------------------------


@pytest.fixture
async def clean(world: World, engine: AsyncEngine) -> World:
    """Своя подготовка: открытые байты-заглушки фикстуры world (АР-8) удаляются."""
    agents = [p.agent_id for p in (world.a, world.b, world.c)]
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM partner_messages WHERE couple_id = :c"), {"c": world.couple_id}
        )
        await conn.execute(
            text(
                "DELETE FROM conversation_messages WHERE conversation_id IN "
                "(SELECT id FROM conversations WHERE agent_id = ANY(:a))"
            ),
            {"a": agents},
        )
        await conn.execute(text("DELETE FROM memory_items WHERE agent_id = ANY(:a)"), {"a": agents})
        await conn.execute(
            text("DELETE FROM message_drafts WHERE agent_id = ANY(:a)"), {"a": agents}
        )
    return world


@pytest.fixture
def ring() -> Keyring:
    return kr({"a8": KA, "b8": KB}, "b8")


async def deliver(
    engine: AsyncEngine, ring: Keyring, msg: uuid.UUID, body: str, adapted: bool = True
) -> None:
    async with engine.begin() as conn, AsyncSession(bind=conn) as s:
        await s.execute(text("SET LOCAL ROLE feelings_worker"))
        assert await texts.pipeline_deliver(
            s, ring, msg, body, is_adapted=adapted, tone=1, harsh=False
        )


async def send(
    w: World, sm: SM, engine: AsyncEngine, ring: Keyring, body: str, adapted: str | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    draft = await texts.create_draft(sm, ring, w.a.user_id, w.a.agent_id, body)
    msg = await texts.send_draft(sm, w.a.user_id, draft, 1)
    await deliver(engine, ring, msg, adapted or body, adapted is not None)
    return draft, msg


async def corrupt(engine: AsyncEngine, table: str, column: str, row: uuid.UUID, how: str) -> None:
    async with engine.begin() as conn:
        blob = bytes(
            (
                await conn.execute(text(f"SELECT {column} FROM {table} WHERE id = :i"), {"i": row})
            ).scalar_one()
        )
        if how == "auth":
            new = blob[:-1] + bytes([blob[-1] ^ 0x80])
        elif how == "format":
            new = b"\x07garbage"
        else:  # unknown_key
            new = kr({"zz": KC}, "zz").seal(crypto.MEMORY_TEXT, row, "x")
        await conn.execute(
            text(f"UPDATE {table} SET {column} = :b WHERE id = :i"), {"b": new, "i": row}
        )


async def test_outsider_and_partner_do_not_read_foreign_texts(
    clean: World, sessionmaker: SM, engine: AsyncEngine, ring: Keyring
) -> None:
    """ТЗ §7 / принцип 4: личный чат, память, резюме, профиль и черновики Анны не видят ни
    партнёр (Игорь), ни посторонняя (Ольга); оригинал — только автор и получатель."""
    w, sm = clean, sessionmaker
    a = w.a
    await texts.add_conversation_message(
        sm, ring, a.user_id, a.conversation_id, "owner", "исповедь"
    )
    await texts.add_memory_item(
        sm, ring, a.user_id, a.agent_id, "fact", "owner_dialog", "память", fingerprints=TEST_FP
    )
    assert await texts.set_conversation_summary(sm, ring, a.user_id, a.conversation_id, "резюме")
    assert await texts.set_agent_profile_summary(sm, ring, a.user_id, a.agent_id, "профиль")
    open_draft = await texts.create_draft(sm, ring, a.user_id, a.agent_id, "ещё не отправлено")
    _, msg = await send(w, sm, engine, ring, "грубовато", adapted="мягче")
    for other in (w.b, w.c):
        # У каждого есть свои записи: чтение по чужому id не должно подмешать и их.
        u = other.user_id
        await texts.add_memory_item(
            sm, ring, u, other.agent_id, "fact", "owner_dialog", "своё", fingerprints=TEST_FP
        )
        await texts.add_conversation_message(sm, ring, u, other.conversation_id, "owner", "своё")
        assert await texts.list_conversation_messages(sm, ring, u, a.conversation_id) == []
        assert await texts.list_memory_items(sm, ring, u, a.agent_id) == []
        assert await texts.get_conversation_summary(sm, ring, u, a.conversation_id) is None
        assert await texts.get_agent_profile_summary(sm, ring, u, a.agent_id) is None
        assert await texts.get_draft(sm, ring, u, open_draft) is None
        assert await texts.update_draft_text(sm, ring, u, open_draft, "подмена") is None
        assert not await texts.set_conversation_summary(sm, ring, u, a.conversation_id, "x")
        assert not await texts.set_agent_profile_summary(sm, ring, u, a.agent_id, "x")
        with pytest.raises(DBAPIError):
            await texts.add_memory_item(
                sm, ring, u, a.agent_id, "fact", "owner_dialog", "x", fingerprints=TEST_FP
            )
        with pytest.raises(DBAPIError):
            await texts.add_conversation_message(sm, ring, u, a.conversation_id, "agent", "x")
        with pytest.raises(DBAPIError):
            await texts.send_draft(sm, u, open_draft, 1)
    # Партнёр в ленте видит только версию для получателя, не оригинал; посторонняя — ничего.
    feed_b = {f.id: f for f in await texts.read_feed(sm, ring, w.b.user_id)}
    assert feed_b[msg].text == "мягче" and feed_b[msg].is_adapted and not feed_b[msg].outgoing
    assert all(f.id != msg for f in await texts.read_feed(sm, ring, w.c.user_id))
    assert await texts.read_original(sm, ring, w.c.user_id, msg) is None
    # Черновик Анны не изменён попытками, её данные читаются ей.
    got = await texts.get_draft(sm, ring, a.user_id, open_draft)
    assert got is not None and got.text == "ещё не отправлено" and got.version == 1
    assert await texts.get_conversation_summary(sm, ring, a.user_id, a.conversation_id) == "резюме"


@pytest.mark.parametrize("how", ["auth", "format", "unknown_key"])
async def test_row_failures_do_not_break_neighbours_or_leak_to_log(
    clean: World,
    sessionmaker: SM,
    engine: AsyncEngine,
    ring: Keyring,
    caplog: pytest.LogCaptureFixture,
    how: str,
) -> None:
    """Ревью r1 MAJOR-1(а): построчная недоступность в чате, памяти и ленте (обе стороны)."""
    w, sm, a = clean, sessionmaker, clean.a
    tag = uuid.uuid4().hex
    chat = [
        await texts.add_conversation_message(
            sm, ring, a.user_id, a.conversation_id, "owner", f"чат{i} {tag}"
        )
        for i in range(3)
    ]
    mem = [
        await texts.add_memory_item(
            sm,
            ring,
            a.user_id,
            a.agent_id,
            "fact",
            "owner_dialog",
            f"память{i} {tag}",
            fingerprints=TEST_FP,
        )
        for i in range(3)
    ]
    sent = [
        await send(w, sm, engine, ring, f"ориг{i} {tag}", adapted=f"мягко{i} {tag}")
        for i in range(3)
    ]
    await corrupt(engine, "conversation_messages", "text_enc", chat[1], how)
    await corrupt(engine, "memory_items", "text_enc", mem[1], how)
    await corrupt(engine, "partner_messages", "original_enc", sent[1][1], how)  # у автора
    await corrupt(engine, "partner_messages", "adapted_enc", sent[2][1], how)  # у получателя
    caplog.set_level(logging.DEBUG)

    got_chat = await texts.list_conversation_messages(sm, ring, a.user_id, a.conversation_id)
    assert [m.id for m in got_chat] == chat  # порядок и состав сохранены
    assert [(m.text, m.unavailable) for m in got_chat] == [
        (f"чат0 {tag}", None),
        (None, how),
        (f"чат2 {tag}", None),
    ]
    got_mem = await texts.list_memory_items(sm, ring, a.user_id, a.agent_id)
    assert [(m.id, m.text, m.unavailable) for m in got_mem] == [
        (mem[0], f"память0 {tag}", None),
        (mem[1], None, how),
        (mem[2], f"память2 {tag}", None),
    ]
    feed_a = {f.id: f for f in await texts.read_feed(sm, ring, a.user_id)}
    assert (feed_a[sent[0][1]].text, feed_a[sent[0][1]].unavailable) == (f"ориг0 {tag}", None)
    assert (feed_a[sent[1][1]].text, feed_a[sent[1][1]].unavailable) == (None, how)
    assert feed_a[sent[2][1]].text == f"ориг2 {tag}"  # испорчена версия получателя, не оригинал
    feed_b = {f.id: f for f in await texts.read_feed(sm, ring, w.b.user_id)}
    assert feed_b[sent[1][1]].text == f"мягко1 {tag}"
    assert (feed_b[sent[2][1]].text, feed_b[sent[2][1]].unavailable) == (None, how)

    assert f"reason={how}" in caplog.text
    secrets = [
        tag,
        str(a.user_id),
        str(w.b.user_id),
        str(a.conversation_id),
        str(a.agent_id),
        str(w.couple_id),
        str(chat[1]),
        str(mem[1]),
        str(sent[1][0]),
        str(sent[1][1]),
        str(sent[2][1]),
        b64(KA),
        b64(KB),
    ]
    for s in secrets:
        assert s not in caplog.text, s
    for rec in caplog.records:
        assert all(tag not in str(arg) for arg in (rec.args or ()))


@pytest.mark.parametrize("how", ["auth", "format", "unknown_key", "context"])
async def test_original_view_not_marked_when_decryption_fails(
    clean: World, sessionmaker: SM, engine: AsyncEngine, ring: Keyring, how: str
) -> None:
    """Ревью r1 MINOR-3 / ТЗ §7 «факт просмотра оригинала»: отметка — только при показе."""
    w, sm = clean, sessionmaker
    draft, msg = await send(w, sm, engine, ring, "оригинал для просмотра", adapted="мягко")
    async with engine.connect() as conn:
        good_blob = (
            await conn.execute(
                text("SELECT original_enc FROM partner_messages WHERE id = :m"), {"m": msg}
            )
        ).scalar_one()
    if how == "context":
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE partner_messages SET draft_id = NULL WHERE id = :m"), {"m": msg}
            )
    else:
        await corrupt(engine, "partner_messages", "original_enc", msg, how)

    async def viewed() -> object:
        async with engine.connect() as conn:
            return (
                await conn.execute(
                    text("SELECT original_viewed_at FROM partner_messages WHERE id = :m"),
                    {"m": msg},
                )
            ).scalar_one()

    with pytest.raises(DecryptionError) as exc:
        await texts.read_original(sm, ring, w.b.user_id, msg)
    assert exc.value.reason == how
    assert "оригинал" not in str(exc.value)
    assert await viewed() is None
    # Починили запись — показ засчитывается.
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE partner_messages SET original_enc = :b, draft_id = :d WHERE id = :m"),
            {"b": good_blob, "d": draft, "m": msg},
        )
    assert await texts.read_original(sm, ring, w.b.user_id, msg) == "оригинал для просмотра"
    assert await viewed() is not None


async def test_major1_sent_draft_and_agent_cannot_be_deleted_by_app(
    clean: World, sessionmaker: SM, engine: AsyncEngine, ring: Keyring
) -> None:
    """Ревью r1 MAJOR-1(б) / Б-38 (миграция 0005): приложение не может потерять контекст
    оригинала — отправленный черновик не удаляется и не «откатывается», агент не удаляется,
    `draft_id` сообщения напрямую не меняется."""
    w, sm, a = clean, sessionmaker, clean.a
    draft, msg = await send(w, sm, engine, ring, "останется читаемым")
    async with sm() as s, s.begin():
        await s.execute(text("SET LOCAL ROLE feelings_app"))
        await s.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(a.user_id)})
        r1 = await s.execute(
            text("UPDATE message_drafts SET status = 'draft' WHERE id = :d"), {"d": draft}
        )
        r2 = await s.execute(text("DELETE FROM message_drafts WHERE id = :d"), {"d": draft})
        assert r1.rowcount == 0 and r2.rowcount == 0
    for sql in (
        "DELETE FROM agents WHERE id = :x",
        "UPDATE partner_messages SET draft_id = NULL WHERE id = :x",
    ):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with sm() as s, s.begin():
                await s.execute(text("SET LOCAL ROLE feelings_app"))
                await s.execute(
                    text("SELECT set_config('app.user_id', :u, true)"), {"u": str(a.user_id)}
                )
                await s.execute(text(sql), {"x": a.agent_id if "agents" in sql else msg})
    feed = {f.id: f for f in await texts.read_feed(sm, ring, a.user_id)}
    assert feed[msg].text == "останется читаемым"
    assert await texts.read_original(sm, ring, w.b.user_id, msg) == "останется читаемым"


# --- 7. Критерий RoadMap: открытого текста нет ни в одной колонке БД и в журналах -------------


async def all_columns_hits(engine: AsyncEngine, needle: str | bytes) -> list[str]:
    """Перебор ВСЕХ колонок всех таблиц пользовательских схем без списка типов: bytea —
    побайтно, всё остальное — через приведение к text."""
    raw = needle if isinstance(needle, bytes) else needle.encode()
    hits = []
    async with engine.connect() as conn:
        cols = (
            await conn.execute(
                text(
                    "SELECT n.nspname, c.relname, a.attname, format_type(a.atttypid, NULL) AS t "
                    "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relkind IN ('r', 'p', 'm') AND a.attnum > 0 AND NOT a.attisdropped "
                    "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
                    "AND n.nspname NOT LIKE 'pg_toast%'"
                )
            )
        ).all()
        assert len(cols) > 50
        for schema, table, column, typ in cols:
            if typ == "bytea":
                cond, param = f'position(CAST(:n AS bytea) IN "{column}") > 0', raw
            else:
                if isinstance(needle, bytes):
                    continue
                cond, param = f'strpos(CAST("{column}" AS text), :n) > 0', needle
            n = (
                await conn.execute(
                    text(f'SELECT count(*) FROM "{schema}"."{table}" WHERE {cond}'), {"n": param}
                )
            ).scalar_one()
            if n:
                hits.append(f"{table}.{column}")
    return hits


async def test_search_control_finds_plaintext(clean: World, engine: AsyncEngine) -> None:
    tag = "контроль-" + uuid.uuid4().hex
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE agents SET profile_summary_enc = :t WHERE id = :a"),
            {"t": f"x{tag}y".encode(), "a": clean.c.agent_id},
        )
        await conn.execute(
            text("UPDATE users SET name = :n WHERE id = :u"), {"n": tag, "u": clean.c.user_id}
        )
    assert sorted(await all_columns_hits(engine, tag)) == [
        "agents.profile_summary_enc",
        "users.name",
    ]


async def test_no_plaintext_anywhere_after_all_seven_texts(
    clean: World,
    sessionmaker: SM,
    engine: AsyncEngine,
    ring: Keyring,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ТЗ §9.1 п.3, п.5, §12 п.7: все семь колонок `*_enc` записаны через слой доступа — ни
    один текст (и ни одно значение ключа, ТЗ §5) не найден ни в одной колонке и в журналах."""
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.INFO, logger="sqlalchemy.engine")
    w, sm, a = clean, sessionmaker, clean.a
    tag = uuid.uuid4().hex
    t = {
        k: f"{k} мне тяжело {tag}"
        for k in ("chat", "csum", "prof", "mem", "draft", "orig", "adapt")
    }
    chat = await texts.add_conversation_message(
        sm, ring, a.user_id, a.conversation_id, "owner", t["chat"]
    )
    assert await texts.set_conversation_summary(sm, ring, a.user_id, a.conversation_id, t["csum"])
    assert await texts.set_agent_profile_summary(sm, ring, a.user_id, a.agent_id, t["prof"])
    mem = await texts.add_memory_item(
        sm,
        ring,
        a.user_id,
        a.agent_id,
        "sensitive_topic",
        "owner_dialog",
        t["mem"],
        fingerprints=TEST_FP,
    )
    unsent = await texts.create_draft(sm, ring, a.user_id, a.agent_id, t["draft"])
    d2 = await texts.create_draft(sm, ring, a.user_id, a.agent_id, "черновик-v1")
    assert await texts.update_draft_text(sm, ring, a.user_id, d2, t["orig"]) == 2
    msg = await texts.send_draft(sm, a.user_id, d2, 2)
    async with engine.begin() as conn, AsyncSession(bind=conn) as s:
        await s.execute(text("SET LOCAL ROLE feelings_worker"))
        assert await texts.pipeline_read_original(s, ring, msg, d2) == t["orig"]
        assert await texts.pipeline_deliver(
            s, ring, msg, t["adapt"], is_adapted=True, tone=1, harsh=False
        )
    assert await texts.read_original(sm, ring, w.b.user_id, msg) == t["orig"]

    # Все семь колонок заполнены шифротекстом под текущим ключом.
    async with engine.connect() as conn:
        blobs = {
            "conversation_messages.text_enc": (
                await conn.execute(
                    text("SELECT text_enc FROM conversation_messages WHERE id=:i"), {"i": chat}
                )
            ).scalar_one(),
            "conversations.summary_enc": (
                await conn.execute(
                    text("SELECT summary_enc FROM conversations WHERE id=:i"),
                    {"i": a.conversation_id},
                )
            ).scalar_one(),
            "agents.profile_summary_enc": (
                await conn.execute(
                    text("SELECT profile_summary_enc FROM agents WHERE id=:i"), {"i": a.agent_id}
                )
            ).scalar_one(),
            "memory_items.text_enc": (
                await conn.execute(
                    text("SELECT text_enc FROM memory_items WHERE id=:i"), {"i": mem}
                )
            ).scalar_one(),
            "message_drafts.text_enc": (
                await conn.execute(
                    text("SELECT text_enc FROM message_drafts WHERE id=:i"), {"i": unsent}
                )
            ).scalar_one(),
            "partner_messages.original_enc": (
                await conn.execute(
                    text("SELECT original_enc FROM partner_messages WHERE id=:i"), {"i": msg}
                )
            ).scalar_one(),
            "partner_messages.adapted_enc": (
                await conn.execute(
                    text("SELECT adapted_enc FROM partner_messages WHERE id=:i"), {"i": msg}
                )
            ).scalar_one(),
        }
    assert all(b is not None and crypto.key_id_of(b) == "b8" for b in blobs.values())
    # app_send_draft перенёс шифротекст черновика байт в байт (непрозрачные байты, карточка).
    async with engine.connect() as conn:
        sent_draft = (
            await conn.execute(text("SELECT text_enc FROM message_drafts WHERE id = :i"), {"i": d2})
        ).scalar_one()
    assert bytes(sent_draft) == bytes(blobs["partner_messages.original_enc"])
    assert (
        AESGCM(KB)
        .decrypt(
            bytes(sent_draft)[4:16],
            bytes(sent_draft)[16:],
            header("b8") + context("message_drafts", "text_enc", d2),
        )
        .decode()
        == t["orig"]
    )

    for needle in [tag, *t.values(), "мне тяжело", "черновик-v1"]:
        assert await all_columns_hits(engine, needle) == [], needle
    for key in (KA, KB):
        assert await all_columns_hits(engine, key) == []
        assert await all_columns_hits(engine, b64(key)) == []
        assert await all_columns_hits(engine, key.hex()) == []
    assert "INSERT INTO conversation_messages" in caplog.text  # SQL-журнал включён
    for value in [tag, *t.values(), "черновик-v1", b64(KA), b64(KB)]:
        assert value not in caplog.text

"""Шифрование текстов при хранении (ТЗ §9.1 п.3, задача 4.2).

AES-256-GCM (`cryptography`), шифрование на уровне приложения: в колонки `*_enc` (bytea, АР-8)
попадает только шифротекст, ключи — только из окружения, отдельно от базы (ТЗ §5).

Формат шифротекста (версия 1):

    0x01 | len(key_id) (1 байт) | key_id (ASCII) | nonce (12 байт) | шифротекст + тег (16 байт)

- nonce — 96 бит из `os.urandom` на каждую запись;
- `key_id` — какой ключ из набора применён: новые записи — текущим, чтение — любым (ротация);
- связанные данные GCM = заголовок (версия, длина и id ключа) + контекст «таблица|колонка|id»
  (`Field.aad`): шифротекст, переставленный в другую строку или колонку, не расшифровывается.

Любая ошибка (подмена байта, чужие связанные данные, чужой или неизвестный ключ, обрезка) —
`DecryptionError` с кодом причины; ни ключ, ни открытый текст в сообщения ошибок не попадают.
Модуль ничего не пишет в журналы.

Ключи: `FEELINGS_ENCRYPTION_KEYS` — `id:base64url(32 байта)` через запятую,
`FEELINGS_ENCRYPTION_KEY_ID` — id текущего ключа. Без ключей сервис не стартует
(`load_keyring` в `app.main`)."""

import base64
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import ConfigError, Settings

FORMAT_VERSION = 1
KEY_LEN = 32
NONCE_LEN = 12
TAG_LEN = 16
_KEY_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
_B64URL = re.compile(r"[A-Za-z0-9_-]+={0,2}")
_IDENT = re.compile(r"[a-z][a-z0-9_]*")


class KeyConfigError(ConfigError):
    """Неверный набор ключей. В тексте — только имя переменной и id ключа, не значение."""


# Код причины: контекст (id строки для связанных данных) потерян — например, удалён черновик
# отправленного сообщения (BACKLOG Б-38). Отличается от `auth` — подделки или чужого ключа.
CONTEXT_MISSING = "context"


class DecryptionError(Exception):
    """Шифротекст отвергнут. `reason`: format, unknown_key, auth, encoding, context."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"ciphertext rejected: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class Field:
    """Колонка с зашифрованным текстом; связанные данные — «таблица|колонка|id строки»."""

    table: str
    column: str

    def __post_init__(self) -> None:
        if not (_IDENT.fullmatch(self.table) and _IDENT.fullmatch(self.column)):
            raise ValueError("table and column must be lowercase identifiers")

    def aad(self, row_id: uuid.UUID) -> bytes:
        if not isinstance(row_id, uuid.UUID):
            raise TypeError("row_id must be uuid.UUID")
        return f"{self.table}|{self.column}|{row_id}".encode("ascii")


# Все зашифрованные тексты схемы (АР-8). id строки для связанных данных — id этой строки,
# кроме оригинала сообщения пары: см. MESSAGE_ORIGINAL.
DRAFT_TEXT = Field("message_drafts", "text_enc")
# `app_send_draft` копирует `message_drafts.text_enc` в `partner_messages.original_enc` как
# есть (функция БД ключа не знает), а id новой строки создаёт сама. Поэтому оригинал
# расшифровывается с контекстом черновика: «message_drafts|text_enc|<partner_messages.draft_id>».
# Перестановка между сообщениями всё равно отвергается — у каждого сообщения свой черновик.
MESSAGE_ORIGINAL = DRAFT_TEXT
MESSAGE_ADAPTED = Field("partner_messages", "adapted_enc")
CONVERSATION_MESSAGE_TEXT = Field("conversation_messages", "text_enc")
MEMORY_TEXT = Field("memory_items", "text_enc")
CONVERSATION_SUMMARY = Field("conversations", "summary_enc")
AGENT_PROFILE_SUMMARY = Field("agents", "profile_summary_enc")


def _header(key_id: str) -> bytes:
    kid = key_id.encode("ascii")
    return bytes((FORMAT_VERSION, len(kid))) + kid


def _split(blob: bytes) -> tuple[bytes, str, bytes, bytes]:
    """→ (заголовок, id ключа, nonce, шифротекст с тегом)."""
    if len(blob) < 2 or blob[0] != FORMAT_VERSION:
        raise DecryptionError("format")
    kid_len = blob[1]
    head_len = 2 + kid_len
    if kid_len == 0 or len(blob) < head_len + NONCE_LEN + TAG_LEN:
        raise DecryptionError("format")
    kid_raw = blob[2:head_len]
    try:
        key_id = kid_raw.decode("ascii")
    except UnicodeDecodeError:
        raise DecryptionError("format") from None
    if not _KEY_ID.fullmatch(key_id):
        raise DecryptionError("format")
    return (
        blob[:head_len],
        key_id,
        blob[head_len : head_len + NONCE_LEN],
        blob[head_len + NONCE_LEN :],
    )


class Keyring:
    """Набор ключей: шифрует текущим, расшифровывает любым из набора."""

    __slots__ = ("_aead", "_current")

    def __init__(self, keys: Mapping[str, bytes], current_key_id: str) -> None:
        if not keys:
            raise KeyConfigError("encryption keyring is empty")
        for key_id, key in keys.items():
            if not _KEY_ID.fullmatch(key_id):
                raise KeyConfigError("encryption key id must be 1-32 characters A-Z a-z 0-9 _ -")
            if len(key) != KEY_LEN:
                raise KeyConfigError(f"encryption key {key_id!r} must be {KEY_LEN} bytes")
        if current_key_id not in keys:
            raise KeyConfigError("FEELINGS_ENCRYPTION_KEY_ID is not in FEELINGS_ENCRYPTION_KEYS")
        self._aead = {key_id: AESGCM(bytes(key)) for key_id, key in keys.items()}
        self._current = current_key_id

    def __repr__(self) -> str:
        return f"Keyring(current={self._current!r}, key_ids={sorted(self._aead)!r})"

    @property
    def current_key_id(self) -> str:
        return self._current

    @property
    def key_ids(self) -> frozenset[str]:
        return frozenset(self._aead)

    def encrypt(self, plaintext: bytes, aad: bytes) -> bytes:
        header = _header(self._current)
        nonce = os.urandom(NONCE_LEN)
        sealed = self._aead[self._current].encrypt(nonce, plaintext, header + aad)
        return header + nonce + sealed

    def decrypt(self, blob: bytes, aad: bytes) -> bytes:
        header, key_id, nonce, sealed = _split(bytes(blob))
        aead = self._aead.get(key_id)
        if aead is None:
            raise DecryptionError("unknown_key")
        try:
            return aead.decrypt(nonce, sealed, header + aad)
        except InvalidTag:
            raise DecryptionError("auth") from None

    def reencrypt(self, blob: bytes, aad: bytes) -> bytes:
        """Ротация (4.7): запись под старым ключом → под текущим. Запись уже под текущим
        ключом возвращается как есть, но только после проверки подлинности."""
        plaintext = self.decrypt(blob, aad)
        if key_id_of(blob) == self._current:
            return bytes(blob)
        return self.encrypt(plaintext, aad)

    def needs_rotation(self, blob: bytes) -> bool:
        return key_id_of(blob) != self._current

    # Помощники для слоя доступа: текст ↔ шифротекст колонки `field` строки `row_id`.
    def seal(self, field: Field, row_id: uuid.UUID, value: str) -> bytes:
        return self.encrypt(value.encode("utf-8"), field.aad(row_id))

    def open(self, field: Field, row_id: uuid.UUID, blob: bytes) -> str:
        raw = self.decrypt(blob, field.aad(row_id))
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise DecryptionError("encoding") from None

    def open_optional(self, field: Field, row_id: uuid.UUID, blob: bytes | None) -> str | None:
        return None if blob is None else self.open(field, row_id, blob)

    def reseal(self, field: Field, row_id: uuid.UUID, blob: bytes) -> bytes:
        return self.reencrypt(blob, field.aad(row_id))


def key_id_of(blob: bytes) -> str:
    """id ключа из заголовка (без расшифровки) — для отбора записей на перешифровку."""
    return _split(bytes(blob))[1]


def _decode_key(key_id: str, value: str) -> bytes:
    if not _B64URL.fullmatch(value):
        raise KeyConfigError(f"encryption key {key_id!r} is not base64url")
    try:
        key = base64.urlsafe_b64decode(value.rstrip("=") + "=" * (-len(value.rstrip("=")) % 4))
    except ValueError:
        raise KeyConfigError(f"encryption key {key_id!r} is not base64url") from None
    if len(key) != KEY_LEN:
        raise KeyConfigError(f"encryption key {key_id!r} must be {KEY_LEN} bytes")
    return key


def parse_keyring(raw_keys: str, current_key_id: str) -> Keyring:
    """`id:base64url,id:base64url` + id текущего ключа → Keyring или KeyConfigError."""
    keys: dict[str, bytes] = {}
    for entry in (e.strip() for e in raw_keys.split(",")):
        if not entry:
            continue
        key_id, sep, value = entry.partition(":")
        key_id, value = key_id.strip(), value.strip()
        if not sep or not _KEY_ID.fullmatch(key_id):
            raise KeyConfigError(
                "FEELINGS_ENCRYPTION_KEYS entries must be id:base64url, "
                "id of 1-32 characters A-Z a-z 0-9 _ -"
            )
        if key_id in keys:
            raise KeyConfigError(f"encryption key id {key_id!r} is repeated")
        keys[key_id] = _decode_key(key_id, value)
    if not keys:
        raise KeyConfigError("FEELINGS_ENCRYPTION_KEYS is empty")
    if not current_key_id.strip():
        raise KeyConfigError("FEELINGS_ENCRYPTION_KEY_ID is empty")
    return Keyring(keys, current_key_id.strip())


def load_keyring(settings: Settings) -> Keyring:
    """Проверка при старте: без ключей или с ключом неверной длины сервис не стартует."""
    return parse_keyring(settings.encryption_keys.get_secret_value(), settings.encryption_key_id)


def generate_key_entry(key_id: str) -> str:
    """Новая строка набора `id:base64url` (для `.env`; ключ печатается только локально)."""
    if not _KEY_ID.fullmatch(key_id):
        raise ValueError("key id must be 1-32 characters A-Z a-z 0-9 _ -")
    return f"{key_id}:{base64.urlsafe_b64encode(os.urandom(KEY_LEN)).decode().rstrip('=')}"

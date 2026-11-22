"""Отпечатки записей памяти (ТЗ §6.3 п.4, §2 п.6; задача 2.4; формат — DECISIONS АР-36).

Отпечаток — ``HMAC-SHA-256(K, "memfp1" ‖ 0x1F ‖ agent_id ‖ 0x1F ‖ norm(текст))`` (UTF-8,
``agent_id`` — текст uuid в нижнем регистре). ``norm``: NFKC → casefold → «ё»→«е» → каждый
символ вне категорий Unicode L*/N* → пробел → схлопнуть пробелы → обрезать края. Тип записи в
отпечаток не входит. Ключ ``K`` — отдельный секрет окружения ``FEELINGS_MEMORY_FINGERPRINT_KEY``
(base64url, не короче 32 байт), не ключ шифрования: отпечаток удалённой записи пересчитать
нельзя (текста нет), поэтому прежние ключи остаются для проверки
(``FEELINGS_MEMORY_FINGERPRINT_KEYS_OLD`` — через запятую). Новые записи — текущим ключом,
проверка «не удалена ли» — отпечатками по всем ключам.

Отпечаток ловит только совпадение нормализованного текста (регистр, «ё», пунктуация, пробелы,
совместимые формы Unicode); перефразировку — нет (АР-36)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.config import ConfigError, Settings

PREFIX = b"memfp1"
SEP = b"\x1f"
MIN_KEY_LEN = 32
_SPACES = re.compile(r"\s+")


class FingerprintConfigError(ConfigError):
    """Нет ключа отпечатков или он короче 32 байт / не base64url."""


def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    value = "".join(ch if unicodedata.category(ch)[0] in ("L", "N") else " " for ch in value)
    return _SPACES.sub(" ", value).strip()


def fingerprint(key: bytes, agent_id: uuid.UUID, text: str) -> bytes:
    message = SEP.join((PREFIX, str(agent_id).lower().encode(), normalize(text).encode()))
    return hmac.new(key, message, hashlib.sha256).digest()


@dataclass(frozen=True)
class FingerprintKeys:
    """Текущий ключ и прежние (только для проверки). ``repr`` ключей не показывает."""

    current: bytes = field(repr=False)
    previous: tuple[bytes, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        for key in (self.current, *self.previous):
            if len(key) < MIN_KEY_LEN:
                raise FingerprintConfigError(f"ключ отпечатков короче {MIN_KEY_LEN} байт")

    def of(self, agent_id: uuid.UUID, text: str) -> bytes:
        """Отпечаток для записи (текущий ключ)."""
        return fingerprint(self.current, agent_id, text)

    def all_of(self, agent_id: uuid.UUID, text: str) -> list[bytes]:
        """Отпечатки текста по всем ключам — для проверки удалённых и дубликатов."""
        return [fingerprint(k, agent_id, text) for k in (self.current, *self.previous)]


def _decode(value: str) -> bytes:
    value = value.strip()
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        raise FingerprintConfigError("ключ отпечатков — не base64url") from None


def parse_fingerprint_keys(current: str, previous: Sequence[str] = ()) -> FingerprintKeys:
    if not current.strip():
        raise FingerprintConfigError("FEELINGS_MEMORY_FINGERPRINT_KEY не задан")
    return FingerprintKeys(_decode(current), tuple(_decode(p) for p in previous if p.strip()))


def load_fingerprint_keys(settings: Settings) -> FingerprintKeys:
    old = settings.memory_fingerprint_keys_old.get_secret_value()
    return parse_fingerprint_keys(
        settings.memory_fingerprint_key.get_secret_value(), [p for p in old.split(",") if p]
    )

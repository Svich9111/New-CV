"""Эмбеддинги для поиска памяти «по смыслу» (ТЗ §6.1 п.3, §7: pgvector; задача 2.1).

``Embedder`` — интерфейс модели эмбеддингов. Настоящая модель ещё не выбрана (АР-5: размерность
1024 — допущение; выбор — A3/оператор, без кредитов не проверить), поэтому в коде есть только:

- ``HashingEmbedder`` — детерминированная заглушка без сети: хеширование символьных триграмм
  слов в вектор размерности ``EMBEDDING_DIM``. Это **не** модель смысла: она ловит совпадение
  слов и их общих частей («деньги» / «денег»), что достаточно для тестов и локальной работы;
- ``None`` вместо эмбеддера — поиска по смыслу нет, память берётся по свежести.

Вектор запроса и векторы памяти должны быть сделаны одной моделью: смена модели требует
пересчёта всех векторов (АР-5). В тексты ошибок и журналы тексты не попадают (CLAUDE.md)."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from app.models.base import EMBEDDING_DIM

_WORD = re.compile(r"\w+", re.UNICODE)


class EmbeddingError(Exception):
    """Эмбеддинг не получен (сеть, лимит, неверная размерность). Сборка контекста при этой
    ошибке не падает: память берётся по свежести (``context.build_context``)."""


@runtime_checkable
class Embedder(Protocol):
    """Модель эмбеддингов. ``model`` — идентификатор (по нему видно, чем сделан вектор)."""

    @property
    def model(self) -> str: ...

    @property
    def dim(self) -> int: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Векторы в порядке ``texts``; ошибки — ``EmbeddingError``."""
        ...


class HashingEmbedder:
    """Заглушка без сети: мешок символьных триграмм слов (со знаком хеша), норма L2 = 1."""

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        if dim <= 0:
            raise ValueError("dim")
        self._dim = dim

    @property
    def model(self) -> str:
        return f"stub-hashing-trigram-{self._dim}"

    @property
    def dim(self) -> int:
        return self._dim

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for word in _WORD.findall(text.lower()):
            padded = f"#{word}#"
            grams = [padded[i : i + 3] for i in range(max(len(padded) - 2, 1))]
            for gram in grams:
                digest = hashlib.blake2b(gram.encode(), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % self._dim
                sign = 1.0 if digest[4] & 1 else -1.0
                vec[index] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Косинусная близость (для ранжирования в приложении, без БД)."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def check_dim(vector: Sequence[float], dim: int = EMBEDDING_DIM) -> list[float]:
    """Вектор нужной размерности из конечных чисел — иначе ``EmbeddingError``."""
    if len(vector) != dim:
        raise EmbeddingError(f"размерность вектора {len(vector)}, нужна {dim}")
    out = [float(v) for v in vector]
    if not all(math.isfinite(v) for v in out):
        raise EmbeddingError("в векторе не числа")
    return out


def to_pgvector(vector: Sequence[float]) -> str:
    """Литерал pgvector ``[x1,x2,...]`` для ``CAST(:v AS vector)``."""
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"

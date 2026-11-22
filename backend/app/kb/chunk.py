"""Нормализация и разбиение текста материала на фрагменты (ТЗ §6.4; задача 2.6).

Параметры (``ChunkParams``) и почему такие:

- ``target_chars`` = 800 — фрагмент около 120–150 слов: в контекст агента идут 3–5 фрагментов
  (§6.4), каждый обрезается до ``ContextBudget.item_chars`` = 800 (2.1) — фрагмент не теряет хвост;
- ``max_chars`` = 1200 — жёсткий предел: длинное предложение режется по словам;
- ``overlap_chars`` = 150 — последние предложения фрагмента (не больше ~150 символов) повторяются
  в начале следующего: мысль на стыке не теряется при поиске;
- границы: фрагмент закрывается на конце абзаца, если набрал не меньше ``min_close`` (60 %
  целевого), иначе продолжает набор следующим абзацем; внутри абзаца — по границам предложений.

Нормализация: Unicode NFC, без мягких переносов и нулевой ширины, перенос слова на конце строки
склеивается («сло-\\nво»), одиночный перевод строки внутри абзаца — пробел, пробелы схлопываются."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_INVISIBLE = dict.fromkeys(map(ord, "­​‌‍⁠﻿"))
_HYPHEN_BREAK = re.compile(r"(\w)-\n(?=[a-zа-яё])")
_PARAGRAPH = re.compile(r"\n\s*\n")
_SPACES = re.compile(r"[ \t\f\v ]+")
_SENTENCE = re.compile(r"(?<=[.!?…])\s+(?=[\"«(\[A-ZА-ЯЁ0-9—–-])")


@dataclass(frozen=True)
class ChunkParams:
    target_chars: int = 800
    max_chars: int = 1200
    overlap_chars: int = 150

    @property
    def min_close(self) -> int:
        return int(self.target_chars * 0.6)


def normalize_text(text: str) -> list[str]:
    """Абзацы нормализованного текста (пустые выброшены)."""
    text = unicodedata.normalize("NFC", text).translate(_INVISIBLE)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HYPHEN_BREAK.sub(r"\1", text)
    out = []
    for para in _PARAGRAPH.split(text):
        line = _SPACES.sub(" ", para.replace("\n", " ")).strip()
        if line:
            out.append(line)
    return out


def _sentences(paragraph: str, max_chars: int) -> list[str]:
    out: list[str] = []
    for sentence in _SENTENCE.split(paragraph):
        sentence = sentence.strip()
        while len(sentence) > max_chars:  # слишком длинное — по словам
            cut = sentence.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            out.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            out.append(sentence)
    return out


def split_chunks(text: str, params: ChunkParams | None = None) -> list[str]:
    """Фрагменты по порядку текста."""
    p = params or ChunkParams()
    chunks: list[str] = []
    cur: list[str] = []
    size = 0

    def close() -> None:
        nonlocal cur, size
        if not cur:
            return
        chunks.append(" ".join(cur))
        tail: list[str] = []
        tail_size = 0
        for s in reversed(cur):
            if tail_size + len(s) > p.overlap_chars:
                break
            tail.insert(0, s)
            tail_size += len(s) + 1
        cur = tail if len(tail) < len(cur) else []
        size = sum(len(s) + 1 for s in cur)

    for para in normalize_text(text):
        for sentence in _sentences(para, p.max_chars):
            if cur and size + len(sentence) > p.target_chars:
                close()
                if cur and size + len(sentence) > p.max_chars:
                    cur, size = [], 0
            cur.append(sentence)
            size += len(sentence) + 1
        if size >= p.min_close:
            close()
            cur, size = [], 0  # перекрытие — только внутри абзаца, не через границу
    if cur and (not chunks or " ".join(cur) not in chunks[-1]):
        chunks.append(" ".join(cur))
    return chunks

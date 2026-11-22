"""Запрет цитирования базы знаний (ТЗ §6.4 «пользователю текст книг не цитируется»; Б-67).

Промпт агента уже запрещает цитировать (``dialog.v0``: «knowledge — подсказки из базы знаний —
не цитируй их»); здесь — проверка ответа **в коде**: в ответе не должно быть ``QUOTE_WORDS``
(= 10) слов подряд, совпадающих с фрагментом базы знаний из контекста этого ответа, после
нормализации АР-36 (регистр, «ё», пунктуация, пробелы). Десять слов подряд — не случайное
совпадение общих оборотов («ненасильственное общение», «я-сообщение»), но уже узнаваемая цитата
(примерно одно предложение книги).

Потоковый режим (``QuoteGuard``): ответ показывается кусками, поэтому последние ``QUOTE_WORDS −
1`` слов придерживаются, пока не станет ясно, что с них не начинается цитата (задержка — около
десяти слов, только если в контексте есть фрагменты). Нашлась цитата — показанное остаётся (в
нём цитаты нет), а начиная с неё ответ заменяется безопасной формулировкой ``SAFE_TAIL``: итог
— продолжение уже показанного текста, поэтому замены показанного (которой нет в контракте,
АР-30) не нужно."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from app.core.fingerprint import normalize

QUOTE_WORDS = 10
SAFE_TAIL = (
    "Скажу своими словами и коротко: попробуй назвать своё чувство и то, что тебе сейчас важно, "
    "— с этого легче начать разговор."
)
_WORD = re.compile(r"\w+", re.UNICODE)


def _visible(text: str) -> tuple[str, list[int]]:
    """Текст без невидимых символов (категория Cf: мягкий перенос, нулевая ширина…) и
    комбинируемых знаков (Mn, Me: ударения и т. п.) — их пользователь не видит, а слово они режут
    (тест A8-K1). Второе — исходная позиция каждого оставшегося символа."""
    keep: list[str] = []
    where: list[int] = []
    for i, ch in enumerate(text):
        if unicodedata.category(ch) in ("Cf", "Mn", "Me"):
            continue
        keep.append(ch)
        where.append(i)
    return "".join(keep), where


def _norm_words(text: str) -> list[str]:
    return normalize(_visible(text)[0]).split()


class QuoteIndex:
    """Все последовательности по ``QUOTE_WORDS`` нормализованных слов фрагментов."""

    def __init__(self, fragments: Iterable[str], k: int = QUOTE_WORDS) -> None:
        self.k = k
        self.grams: set[tuple[str, ...]] = set()
        for frag in fragments:
            words = _norm_words(frag)
            for i in range(len(words) - k + 1):
                self.grams.add(tuple(words[i : i + k]))

    def __bool__(self) -> bool:
        return bool(self.grams)


def _spans(text: str) -> list[tuple[int, str]]:
    """(начало слова в исходном тексте, нормализованное слово) — нормализация по слову."""
    clean, where = _visible(text)
    out: list[tuple[int, str]] = []
    for m in _WORD.finditer(clean):
        for w in normalize(m.group()).split():
            out.append((where[m.start()], w))
    return out


def find_quote(text: str, index: QuoteIndex) -> int | None:
    """Позиция (символ) начала первой цитаты в ``text`` или ``None``."""
    if not index:
        return None
    spans = _spans(text)
    k = index.k
    for i in range(len(spans) - k + 1):
        if tuple(w for _, w in spans[i : i + k]) in index.grams:
            return spans[i][0]
    return None


def without_quotes(text: str, index: QuoteIndex) -> tuple[str, bool]:
    """Ответ без цитаты: до цитаты — как есть, дальше — ``SAFE_TAIL``."""
    at = find_quote(text, index)
    if at is None:
        return text, False
    head = text[:at].rstrip()
    return (f"{head} {SAFE_TAIL}" if head else SAFE_TAIL), True


class QuoteGuard:
    """Придержка кусков потока: ``feed`` отдаёт текст, который точно не начало цитаты;
    ``finish`` — остаток (или замену, если цитата найдена). ``shown`` — всё отданное: это и есть
    итоговый текст ответа. ``blocked`` — цитата была."""

    def __init__(self, index: QuoteIndex) -> None:
        self.index = index
        self.text = ""
        self.emitted = 0  # сколько символов ``text`` уже отдано
        self.shown = ""
        self.blocked = False

    def _give(self, out: str) -> str:
        self.shown += out
        return out

    def feed(self, piece: str) -> str:
        if self.blocked:
            return ""
        self.text += piece
        if not self.index:
            out, self.emitted = self.text[self.emitted :], len(self.text)
            return self._give(out)
        spans = _spans(self.text)
        # Последнее слово, возможно, ещё дописывается — в проверку не берём, но придерживаем.
        tail_open = bool(spans) and (self.text[-1].isalnum() or self.text[-1] == "_")
        complete = spans[:-1] if tail_open else spans
        k = self.index.k
        for i in range(len(complete) - k + 1):
            if tuple(w for _, w in complete[i : i + k]) in self.index.grams:
                return self._block(complete[i][0])
        hold_from = complete[-(k - 1)][0] if len(complete) >= k - 1 else 0
        if tail_open:
            hold_from = min(hold_from, spans[-1][0])
        if hold_from <= self.emitted:
            return ""
        out = self.text[self.emitted : hold_from]
        self.emitted = hold_from
        return self._give(out)

    def _block(self, at: int) -> str:
        self.blocked = True
        head = self.text[self.emitted : at]
        self.emitted = len(self.text)
        joined = (self.shown + head).rstrip()
        out = head.rstrip() + (" " if joined else "") + SAFE_TAIL
        if not head.strip() and self.shown and self.shown[-1].isspace():
            out = SAFE_TAIL
        return self._give(out)

    def finish(self) -> str:
        if self.blocked:
            return ""
        at = find_quote(self.text, self.index)
        if at is not None:
            return self._block(at)
        out = self.text[self.emitted :]
        self.emitted = len(self.text)
        return self._give(out)

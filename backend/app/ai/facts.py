"""Сверка чисел оригинала и адаптации кодом (ТЗ §6.2 п.5 «запрещено менять факты», п.6).

Основной контроль смысла — модель (``meaning``); здесь — дешёвая страховка, которая не зависит
от модели. Сравниваются состав чисел и порядок внутри диапазонов («с 5 до 7» → «с 7 до 5»,
«5–7» → «7–5» — расхождение; перестановка независимых чисел в предложении — нет):

- цифры — как написаны (``３`` и ``3`` — разные, ``1 500`` — два числа: формат не угадывается,
  при сомнении — оригинал с предупреждением, это безопасная сторона);
- время ``19:00`` — одно число ``19`` (минуты ``:00`` не значимы), ``19:30`` — ``19``, ``30``;
- «в 7 вечера / дня» (после предлога времени) → ``19``, «7 утра / ночи» → ``7``; «3 дня» без
  предлога — количество дней, ``3``;
- «полчаса» = «30 минут»; «во-первых / во-вторых» — не числа;
- привязка к соседним словам (по грубой основе: «часа» = «часов»): у слова рядом с числами в
  обоих текстах набор чисел совпадает;
- числительные словами: количественные и порядковые 2–20, десятки, сто («три», «третий раз»,
  «трижды», «двое»), «первый». «Один / одна» не считаются: чаще значат «в одиночку».

Известные ограничения (в сторону предупреждения, то есть оригинала с предупреждением):
составные («двадцать пять» против «25») сверяются по частям; месяцы и даты словами («21 ноября»
против «21.11»); «1 500» против «1500»; гипербола («сто раз» → «много раз»); «полтора часа»
против «90 минут»; лишний или пропавший предлог у одного из нескольких чисел («в 7 и в 9» →
«в 7 и 9»).

Известные пропуски (расхождение не ловится, остаётся модель смысла): обмен чисел, если рядом с
ними в двух текстах нет разных общих слов: «Ты обещал в 20, а пришёл в 23» → «Ты пришёл в 20, а
обещал в 23» (у обоих чисел соседи «в» и «а»); подобные перестройки фразы."""

from __future__ import annotations

import re
from collections import Counter

_ADJ = ("ый", "ой", "ая", "ое", "ого", "ому", "ую", "ом", "ые", "ых", "ым", "ыми", "ий")
_SOFT_ADJ = ("ий", "ья", "ье", "ьего", "ьему", "ью", "ьем", "ьи", "ьих", "ьей", "ьим", "ьими")


def _forms() -> dict[str, int]:
    words: dict[str, int] = {}
    cardinal = {
        2: "два две двух двум двумя дважды двое двоих",
        3: "три трёх трех трём трем тремя трижды трое троих",
        4: "четыре четырёх четырех четырём четырем четырьмя четырежды четверо четверых",
        5: "пять пяти пятью пятеро пятерых",
        6: "шесть шести шестью шестеро",
        7: "семь семи",
        8: "восемь восьми восемью",
        9: "девять девяти девятью",
        10: "десять десяти десятью",
        11: "одиннадцать одиннадцати",
        12: "двенадцать двенадцати",
        13: "тринадцать тринадцати",
        14: "четырнадцать четырнадцати",
        15: "пятнадцать пятнадцати",
        16: "шестнадцать шестнадцати",
        17: "семнадцать семнадцати",
        18: "восемнадцать восемнадцати",
        19: "девятнадцать девятнадцати",
        20: "двадцать двадцати",
        30: "тридцать тридцати",
        40: "сорок сорока",
        50: "пятьдесят пятидесяти",
        60: "шестьдесят шестидесяти",
        70: "семьдесят семидесяти",
        80: "восемьдесят восьмидесяти",
        90: "девяносто девяноста",
        100: "сто ста",
    }
    for value, forms in cardinal.items():
        for form in forms.split():
            words[form] = value
    ordinal = {
        1: "перв",
        2: "втор",
        4: "четвёрт",
        5: "пят",
        6: "шест",
        7: "седьм",
        8: "восьм",
        9: "девят",
        10: "десят",
    }
    for value, stem in ordinal.items():
        for ending in _ADJ:
            words[stem + ending] = value
            words[stem.replace("ё", "е") + ending] = value
    for ending in _SOFT_ADJ:
        words["трет" + ending] = 3
    return words


WORDS = _forms()
_TOKEN = re.compile(r"(\d+)(?::(\d{2}))?|([^\W\d_]+)", re.UNICODE)
_EVENING = {"вечера", "дня"}
# «в 3 дня», «к 7 вечера» — время; «3 дня» без предлога — количество дней (ревью R2-MINOR-4).
_TIME_PREPOSITIONS = {"в", "к", "до", "с", "со", "после", "около", "от", "часов"}
_RANGE_START = {"с", "со", "от"}
_RANGE_END = {"до", "по"}
_NOT_NUMBERS = re.compile(r"\bво-(?:первых|вторых|третьих|четвертых|пятых)\b")
_REWRITES = ((re.compile(r"\bполчаса\b"), "30 минут"), (re.compile(r"\bполчас\b"), "30 минут"))
_DASH_RANGE = re.compile(r"(\d+)\s*[-–—]\s*(\d+)")


_ENDINGS = tuple(
    sorted(
        (
            "ами ями ов ев ей ам ям ах ях ом ем ой ий ый ая яя ое ее ую юю а я ы и у ю о е ь й"
        ).split(),
        key=len,
        reverse=True,
    )
)


def _stem(word: str) -> str:
    """Грубая основа соседнего слова: одно окончание срезается, если остаётся не меньше трёх
    букв («часа» = «часов» = «час», «минуты» = «минут»; О-15). Совпадение основ разных слов
    уводит в сторону предупреждения, а не пропуска."""
    for ending in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            return word[: -len(ending)]
    return word


def _prepare(text: str) -> str:
    text = text.casefold().replace("ё", "е")
    text = _NOT_NUMBERS.sub(" ", text)
    for pattern, replacement in _REWRITES:
        text = pattern.sub(replacement, text)
    return text


Scan = tuple[list[str], list[tuple[str, str]], dict[str, Counter[str]]]


def _scan(text: str) -> Scan:
    """Числа по порядку, диапазоны «с X до Y» / «X–Y» (их порядок значим) и привязка чисел к
    соседним словам: слово сразу перед числом и сразу после него → какие числа рядом с ним."""
    prepared = _prepare(text)
    tokens = list(_TOKEN.finditer(prepared))
    found: list[str] = []
    positions: list[int] = []  # индекс токена каждого числа
    for i, match in enumerate(tokens):
        digits, minutes, word = match.group(1), match.group(2), match.group(3)
        value: str | None = None
        if digits is not None:
            value = digits
            if minutes is not None:
                found.append(value)
                positions.append(i)
                if minutes != "00":
                    found.append(minutes)
                    positions.append(i)
                continue
        elif word is not None:
            number = WORDS.get(word) or WORDS.get(word.replace("е", "ё"))
            if number is not None:
                value = str(number)
        if value is None:
            continue
        previous = tokens[i - 1].group(3) if i > 0 else None
        following = tokens[i + 1].group(3) if i + 1 < len(tokens) else None
        if (
            following in _EVENING
            and previous in _TIME_PREPOSITIONS
            and value.isascii()
            and value.isdigit()
            and int(value) < 12
        ):
            value = str(int(value) + 12)
        found.append(value)
        positions.append(i)
    ranges: list[tuple[str, str]] = []
    for n in range(len(found) - 1):
        start, end = positions[n], positions[n + 1]
        before = tokens[start - 1].group(3) if start > 0 else None
        between = [t.group(3) for t in tokens[start + 1 : end]]
        if before in _RANGE_START and between and between[-1] in _RANGE_END:
            ranges.append((found[n], found[n + 1]))
    ranges += [(a, b) for a, b in _DASH_RANGE.findall(prepared)]
    # Числа подряд без слов между ними («19.30», «2,5», «5–7») — одна группа с общими соседями.
    is_number = [t.group(1) is not None or WORDS.get(t.group(3) or "") is not None for t in tokens]
    anchors: dict[str, Counter[str]] = {}
    for value, index in zip(found, positions, strict=True):
        start = end = index
        while start > 0 and is_number[start - 1]:
            start -= 1
        while end + 1 < len(tokens) and is_number[end + 1]:
            end += 1
        for j in (start - 1, end + 1):
            if 0 <= j < len(tokens) and tokens[j].group(3) is not None:
                anchors.setdefault(_stem(tokens[j].group(3)), Counter())[value] += 1
    return found, ranges, anchors


def numbers(text: str) -> list[str]:
    """Числа текста по порядку (как строки; см. описание модуля)."""
    return _scan(text)[0]


def numbers_differ(original: str, adapted: str) -> bool:
    """Числа оригинала и адаптации расходятся: по составу (мультимножество), по порядку внутри
    диапазонов («с 5 до 7» → «с 7 до 5») или по привязке к соседним словам («2 раза за 3 дня» →
    «3 раза за 2 дня», «пришёл в 7, а ушёл в 9» → «пришёл в 9, а ушёл в 7»). Перестановка частей
    предложения вместе с числами («третий раз за 2 недели» → «за 2 недели третий раз») — не
    расхождение."""
    a_values, a_ranges, a_anchors = _scan(original)
    b_values, b_ranges, b_anchors = _scan(adapted)
    if Counter(a_values) != Counter(b_values) or a_ranges != b_ranges:
        return True
    # Обмен чисел между величинами (A8-10, R3-MINOR-1): у слова, которое стоит рядом с числами в
    # обоих текстах («опоздал 2 раза» → «опоздал 3 раза»), набор чисел должен совпасть.
    return any(a_anchors[w] != b_anchors[w] for w in a_anchors.keys() & b_anchors.keys())

"""Ревью 0.5, раунд 4 (R4-MAJOR-1 = A8-11, R4-MINOR-1, R4-MINOR-2) — сборка кризисного ответа по
набору строк. Исчерпывающий перебор пар и троек (главная, вторичные) × {сообщение, чат} ×
{V0, V2} с инвариантами и полный текст на ключевых сочетаниях. Без сети, все тексты выдуманы."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from app.ai import crisis, prompts

REPO_DIR = Path(__file__).resolve().parents[2]
TYPES = crisis.CRISIS_TYPES
VICTIM_PHRASE = "вина за это не на тебе"
SUGGEST = "подумаем, как сказать"
EMERGENCY_NOW = "немедленно звони в экстренные службы"

# Все непустые наборы строк (1–5) × любая главная строка из набора (раунд 5, R5-MINOR-3);
# независимость от порядка вторичных проверяется перестановками внутри теста.
COMBOS = [
    (main, sorted(set(subset) - {main}))
    for n in range(1, len(TYPES) + 1)
    for subset in itertools.combinations(TYPES, n)
    for main in subset
]
CONTEXTS = [True, False]  # True — остановленное сообщение, False — личный чат
SETS = {"V0": prompts.V0, "V2": prompts.V2}
THREAT_BLOCKS = {
    "outgoing_threat",
    "outgoing_threat_chat",
    "outgoing_threat_mixed",
    "outgoing_threat_mixed_chat",
    "outgoing_threat_brief",
    "outgoing_threat_brief_chat",
}


def b(name: str, version: str = "v2") -> str:
    return prompts.load("crisis", version, block=name).text


def test_combos_are_exhaustive() -> None:
    assert len(COMBOS) == sum(k * len(list(itertools.combinations(TYPES, k))) for k in range(1, 6))
    assert len(COMBOS) == 80


def _names(text: str, version: str, reply: str | None) -> list[str]:
    """Разбор ответа на имена блоков шаблона (каждый блок — дословно)."""
    by_text: dict[str, str] = {}
    for name in {*TYPES, *crisis.LATE_BLOCKS}:
        for v in (version, crisis.LATE_BLOCKS.get(name, version)):
            try:
                by_text[prompts.load("crisis", v, block=name).text] = name
                break
            except prompts.PromptError:
                continue
    parts = text.split("\n\n")
    if reply:
        assert parts[0] == reply
        parts = parts[1:]
    assert all(p in by_text for p in parts), parts
    return [by_text[p] for p in parts]


@pytest.mark.parametrize("set_name", list(SETS))
@pytest.mark.parametrize("withheld", CONTEXTS, ids=["message", "chat"])
@pytest.mark.parametrize(("main", "rest"), COMBOS, ids=lambda x: str(x))
def test_crisis_invariants(main: str, rest: list[str], withheld: bool, set_name: str) -> None:
    present = {main, *rest}
    pset = SETS[set_name]
    r = crisis.respond([main, *rest], primary=main, prompt_set=pset, withheld=withheld)
    low = r.text.casefold()
    names = _names(r.text, pset.crisis, None)
    T, V, L, H, U = (t in present for t in TYPES_ORDER)
    threat_at = [i for i, n in enumerate(names) if n in THREAT_BLOCKS]
    if T:
        assert len(threat_at) == 1
        assert VICTIM_PHRASE not in low and not {"name_it", "no_tolerate"} & r.actions
    if V or L:
        assert SUGGEST not in low and "help_contacts" in r.actions
    if V and not T:
        assert "violence" in names and {"name_it", "no_tolerate"} <= r.actions
    if L:
        assert EMERGENCY_NOW in low and "emergency_services" in r.actions
        first = next(n for n in names if n != "not_sent")  # «немедленно звони» — первым
        assert EMERGENCY_NOW in prompts.load("crisis", "v2", block=first).text.casefold()
    if H:
        assert "self_harm" in names and {"stay_in_conversation", "crisis_line_24h"} <= r.actions
        assert "спокойнее" not in low and SUGGEST not in low
        if T:  # поддержка раньше любого блока отправителю
            assert names.index("self_harm") < threat_at[0]
    if U:
        assert names[-1] == "underage" and "no_access" in r.actions
        if T:
            assert "вернёмся" not in low and "return_to_talk" not in r.actions
    if withheld:
        assert low.count("не отправлено") == 1 and "not_sent" in r.actions
    else:
        assert "не отправлено" not in low and "not_sent" not in r.actions
    # Действия соответствуют тексту.
    if "help_contacts" in r.actions:
        assert "«помощь»" in low
    if "emergency_services" in r.actions:
        assert "экстренн" in low
    assert "no_draft" in r.actions
    # Ответ не зависит от порядка вторичных строк.
    for perm in itertools.permutations(rest):
        other = crisis.respond([main, *perm], primary=main, prompt_set=pset, withheld=withheld)
        assert (other.text, other.actions, other.template_version) == (
            r.text,
            r.actions,
            r.template_version,
        )
    assert r.primary == (main if "underage" not in present else "underage")


TYPES_ORDER = ("outgoing_threat", "violence", "life_threat", "self_harm", "underage")


# ------------------------------------------------------------- полный текст: ключевые случаи


@pytest.mark.parametrize("set_name", list(SETS))
def test_victim_main_with_threat_message(set_name: str) -> None:
    r = crisis.respond(
        ["violence", "outgoing_threat"],
        primary="violence",
        prompt_set=SETS[set_name],
        withheld=True,
    )
    assert r.primary == "violence"
    assert r.text == f"{b('outgoing_threat_mixed')}\n\n{b('author_safety')}"


@pytest.mark.parametrize("set_name", list(SETS))
def test_life_threat_main_with_threat_chat(set_name: str) -> None:
    r = crisis.respond(
        ["life_threat", "outgoing_threat"],
        "Мне очень жаль.",
        primary="life_threat",
        prompt_set=SETS[set_name],
    )
    assert r.text == "\n\n".join(
        ["Мне очень жаль.", b("author_safety_life"), b("outgoing_threat_mixed_chat")]
    )
    assert {"emergency_services", "help_contacts"} <= r.actions


def test_underage_with_threat_chat() -> None:
    r = crisis.respond(["underage", "outgoing_threat"], "Я рядом.")
    assert r.text == "\n\n".join(["Я рядом.", b("outgoing_threat_brief_chat"), b("underage", "v0")])
    assert "вернёмся" not in r.text.casefold() and "return_to_talk" not in r.actions
    assert SUGGEST not in r.text.casefold()


def test_threat_and_self_harm_message_support_first() -> None:
    """Раунд 5 (R5-MAJOR-1): поддержка — первой после not_sent, блок отправителю короткий."""
    r = crisis.respond(["outgoing_threat", "self_harm"], withheld=True)
    assert r.text == "\n\n".join(
        [b("not_sent", "v1"), b("self_harm", "v0"), b("outgoing_threat_brief")]
    )
    assert r.template_version == "v0+not_sent@v1+outgoing_threat_brief@v2"
    low = r.text.casefold()
    assert "спокойнее" not in low and SUGGEST not in low and "return_to_talk" not in r.actions
    assert {"not_sent", "explain_reason", "no_softening", "stay_in_conversation"} <= r.actions


def test_threat_and_self_harm_chat_support_first() -> None:
    r = crisis.respond(["self_harm", "outgoing_threat"], "Я рядом.")
    assert r.text == "\n\n".join(
        ["Я рядом.", b("self_harm", "v0"), b("outgoing_threat_brief_chat")]
    )


def test_violence_alone_has_emergency_action() -> None:
    r = crisis.respond(["violence"], withheld=True)  # О-19: блок велит звонить
    assert "emergency_services" in r.actions and "экстренн" in r.text.casefold()


def test_victim_only_message_unchanged() -> None:
    r = crisis.respond(["life_threat", "violence"], withheld=True)
    assert r.text == "\n\n".join([b("not_sent", "v1"), b("life_threat", "v0"), b("violence", "v0")])
    assert r.template_version == "v0+not_sent@v1"


def test_l5_situations_still_consistent() -> None:
    # l5-07: младше 18 + насилие + самоповреждение — без угрозы, ветка «Б»: отказ последним.
    r = crisis.respond(["self_harm", "violence", "underage"], "Спасибо, что рассказала.")
    assert r.text.startswith("Спасибо, что рассказала.")
    assert r.text.endswith(b("underage", "v0"))
    assert {"no_access", "help_contacts", "name_it", "crisis_line_24h"} <= r.actions


# ------------------------------------------------------------------ R4-MINOR-1, R4-MINOR-2


def test_author_safety_gender_neutral_and_life_unconditional() -> None:
    safety = b("author_safety").casefold()
    for word in (" самому", " самой", "обязан", "обязана"):
        assert word not in f" {safety}"
    life = b("author_safety_life").casefold()
    assert EMERGENCY_NOW in life and "если опасность" not in life
    header = (REPO_DIR / "prompts" / "crisis.v2.md").read_text(encoding="utf-8")
    assert "Правка до утверждения" in header


def test_docs_describe_rule_table() -> None:
    doc = crisis.respond.__doc__ or ""
    assert "Таблица правил" in doc and "author_safety_life" in doc
    readme = (REPO_DIR / "prompts" / "README.md").read_text(encoding="utf-8")
    assert "Таблица правил" in readme and "outgoing_threat_mixed_chat" in readme
    q = (REPO_DIR / "evals" / "QUESTIONS_FOR_PSYCHOLOGIST.md").read_text(encoding="utf-8")
    assert "author_safety_life" in q

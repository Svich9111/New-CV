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

COMBOS = [
    (main, list(rest))
    for n in (1, 2)
    for main in TYPES
    for rest in itertools.permutations([t for t in TYPES if t != main], n)
]
CONTEXTS = [True, False]  # True — остановленное сообщение, False — личный чат
SETS = {"V0": prompts.V0, "V2": prompts.V2}


def b(name: str, version: str = "v2") -> str:
    return prompts.load("crisis", version, block=name).text


def test_combos_are_exhaustive() -> None:
    assert len(COMBOS) == 5 * 4 + 5 * 4 * 3  # все упорядоченные пары и тройки


@pytest.mark.parametrize("set_name", list(SETS))
@pytest.mark.parametrize("withheld", CONTEXTS, ids=["message", "chat"])
@pytest.mark.parametrize(("main", "rest"), COMBOS, ids=lambda x: str(x))
def test_crisis_invariants(main: str, rest: list[str], withheld: bool, set_name: str) -> None:
    present = {main, *rest}
    r = crisis.respond([main, *rest], primary=main, prompt_set=SETS[set_name], withheld=withheld)
    low = r.text.casefold()
    if "outgoing_threat" in present:
        assert VICTIM_PHRASE not in low
        assert not {"name_it", "no_tolerate"} & r.actions
    if present & {"violence", "life_threat"}:
        assert SUGGEST not in low
        assert "help_contacts" in r.actions
    if "life_threat" in present:
        assert EMERGENCY_NOW in low and "emergency_services" in r.actions
    if withheld:
        assert low.count("не отправлено") == 1
        assert "not_sent" in r.actions
    else:
        assert "не отправлено" not in low and "not_sent" not in r.actions
    assert "no_draft" in r.actions
    # Ответ не зависит от порядка вторичных строк.
    other = crisis.respond(
        [main, *reversed(rest)], primary=main, prompt_set=SETS[set_name], withheld=withheld
    )
    assert (other.text, other.actions, other.template_version) == (
        r.text,
        r.actions,
        r.template_version,
    )
    assert r.primary == (main if "underage" not in present else "underage")


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
        ["Мне очень жаль.", b("outgoing_threat_mixed_chat"), b("author_safety_life")]
    )
    assert {"emergency_services", "help_contacts"} <= r.actions


def test_underage_with_threat_chat() -> None:
    r = crisis.respond(["underage", "outgoing_threat"], "Я рядом.")
    assert r.text == "\n\n".join(["Я рядом.", b("outgoing_threat_mixed_chat"), b("underage", "v0")])
    assert SUGGEST not in r.text.casefold()


def test_threat_and_self_harm_message_keeps_v0_threat_block() -> None:
    r = crisis.respond(["outgoing_threat", "self_harm"], withheld=True)
    assert r.text == f"{b('outgoing_threat', 'v0')}\n\n{b('self_harm', 'v0')}"
    assert r.template_version == "v0"


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

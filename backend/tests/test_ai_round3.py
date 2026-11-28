"""Исправления по ревью 0.5, раунд 3 (A6 R3-MAJOR-1, R3-MINOR-1…3; A8-8…A8-10, О-13) — без сети.
Модель — заглушка ``ScriptedProvider``. Все люди и тексты выдуманы."""

from __future__ import annotations

import importlib
import shutil
from pathlib import Path
from typing import Any

import pytest

from app.ai import crisis, dialog, facts, pipeline, prompts
from app.ai.dialog import OwnerContext, Turn
from app.ai.llm import LLMBadOutput, LLMRefusal, LLMUnavailable
from app.ai.models import AgentModels
from app.ai.stub import ScriptedProvider

REPO_DIR = Path(__file__).resolve().parents[2]
TEXT = "Если ещё раз тронешь меня — убью, понял?"
SOFT = "Мне страшно и больно."
VICTIM_PHRASES = ("вина за это не на тебе", "терпеть это не нужно", "твоя жизнь сейчас в опасности")


def block(name: str, version: str = "v2") -> str:
    return prompts.load("crisis", version, block=name).text


def scripted(**answers: Any) -> ScriptedProvider:
    table: dict[str, Any] = {
        "safety": {"category": "none", "secondary": []},
        "tone": {"tone": 2, "signals": []},
        "adapt": {"adapted": SOFT, "reply_hint": "Похоже, автору важно, чтобы его услышали."},
        "meaning": {"problems": []},
        **answers,
    }

    def handler(role: str, payload: dict[str, Any], system: str) -> dict[str, Any]:
        value = table[role]
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        return dict(value)

    return ScriptedProvider(handler)


async def process(
    p: ScriptedProvider, prompt_set: prompts.PromptSet = prompts.V0, text: str = TEXT
) -> pipeline.Delivery:
    return await pipeline.process(
        AgentModels.single(p), pipeline.approve(text, confirmed=True), prompt_set=prompt_set
    )


# ------------------------------------------------------------------------- R3-MAJOR-1


@pytest.mark.parametrize("prompt_set", [prompts.V0, prompts.V2], ids=["V0", "V2"])
@pytest.mark.parametrize("victim", ["violence", "life_threat"])
async def test_mixed_message_keeps_help(prompt_set: prompts.PromptSet, victim: str) -> None:
    p = scripted(safety={"category": "threat_or_insult", "secondary": [victim]})
    delivery = await process(p, prompt_set)
    notice = delivery.author_notice
    # Б-103: при признаке угрозы жизни главная — life_threat (код); блоки от главной не зависят.
    main = "life_threat" if victim == "life_threat" else "outgoing_threat"
    assert notice is not None and notice.primary == main
    safety = "author_safety_life" if victim == "life_threat" else "author_safety"  # раунд 4
    pair = [block("outgoing_threat_mixed"), block(safety)]
    if victim == "life_threat":  # раунд 5: «немедленно звони» — первым (R5-MINOR-2)
        pair.reverse()
    assert notice.text == "\n\n".join(pair)
    text = notice.text.casefold()
    for phrase in (*VICTIM_PHRASES, "подумаем, как сказать"):
        assert phrase not in text
    assert "«помощь»" in text and "экстренные службы" in text
    assert text.count("не отправлено") == 1
    assert {"help_contacts", "emergency_services", "not_sent", "explain_reason"} <= notice.actions
    assert not {"name_it", "no_tolerate"} & notice.actions
    order = (  # раунд 5: при life_threat «немедленно звони» — первым
        f"+{safety}@v2+outgoing_threat_mixed@v2"
        if victim == "life_threat"
        else f"+outgoing_threat_mixed@v2+{safety}@v2"
    )
    expected = f"v0{order}" if prompt_set is prompts.V0 else "v2"
    assert notice.template_version == expected
    assert f"crisis.{expected}" in delivery.prompts


async def test_pure_author_threat_unchanged() -> None:
    delivery = await process(scripted(safety={"category": "threat_or_insult", "secondary": []}))
    assert delivery.author_notice is not None
    assert delivery.author_notice.text == block("outgoing_threat", "v0")
    assert delivery.author_notice.template_version == "v0"


CTX = OwnerContext("Вера", "Олег", "Женаты пять лет")
CHAT = [Turn("owner", "Он меня опять избил. Напишу ему, что убью его, если ещё раз тронет.")]
AGENT = "Мне очень жаль, что тебе так больно."


def chat_provider(secondary: list[str], classifier: str) -> ScriptedProvider:
    return scripted(
        dialog={
            "level": 5,
            "reply": AGENT,
            "draft_ready": True,
            "crisis_type": "outgoing_threat",
            "crisis_secondary": secondary,
        },
        safety={"category": classifier, "secondary": []},
    )


@pytest.mark.parametrize("prompt_set", [prompts.V0, prompts.V2], ids=["V0", "V2"])
async def test_mixed_chat_keeps_help_without_not_sent(prompt_set: prompts.PromptSet) -> None:
    p = chat_provider(["violence"], "violence")
    result = await dialog.reply(AgentModels.single(p), CTX, CHAT, prompt_set=prompt_set)
    assert result.crisis is not None and result.draft is None
    assert result.reply == "\n\n".join(
        [AGENT, block("outgoing_threat_mixed_chat"), block("author_safety")]
    )
    text = result.reply.casefold()
    assert "не отправлено" not in text  # в чате сообщения не было
    for phrase in (*VICTIM_PHRASES, "подумаем, как сказать"):
        assert phrase not in text
    assert {"help_contacts", "emergency_services"} <= result.crisis.actions
    assert "not_sent" not in result.crisis.actions


async def test_pure_chat_threat_without_not_sent() -> None:
    p = chat_provider([], "none")
    result = await dialog.reply(AgentModels.single(p), CTX, CHAT)
    assert result.reply == f"{AGENT}\n\n{block('outgoing_threat_chat')}"
    assert "не отправлено" not in result.reply.casefold()
    assert result.crisis is not None and "not_sent" not in result.crisis.actions


# ------------------------------------------------------------------------- R3-MINOR-3


async def test_refusal_consumes_approval() -> None:
    p = scripted(adapt=[LLMRefusal("нет"), {"adapted": SOFT, "reply_hint": None}])
    msg = pipeline.approve("Ты опять опоздал!", confirmed=True)
    with pytest.raises(LLMRefusal):
        await pipeline.process(AgentModels.single(p), msg)
    with pytest.raises(pipeline.AlreadyProcessed):
        await pipeline.process(AgentModels.single(p), msg)


@pytest.mark.parametrize("exc", [LLMUnavailable("503"), LLMBadOutput("мусор")])
async def test_transient_failure_keeps_approval(exc: Exception) -> None:
    # Ошибка на тоне — не деградация, сообщение не доставляется; одобрение остаётся.
    bad = [exc, exc] if isinstance(exc, LLMBadOutput) else [exc]
    p = scripted(tone=[*bad, {"tone": 0, "signals": []}])
    msg = pipeline.approve("Ты опять опоздал!", confirmed=True)
    with pytest.raises(type(exc)):
        await pipeline.process(AgentModels.single(p), msg)
    assert (await pipeline.process(AgentModels.single(p), msg)).status == "as_is"


def test_backlog_b48_mentions_refusal() -> None:
    text = (REPO_DIR / "docs" / "BACKLOG.md").read_text(encoding="utf-8")
    line = next(x for x in text.splitlines() if x.startswith("| Б-48 |"))
    assert "LLMRefusal" in line and "новое «Отправить»" in line


# ------------------------------------------------------------------- R3-MINOR-1 = A8-10


@pytest.mark.parametrize(
    ("original", "adapted", "differ"),
    [
        ("Ждала 2 часа, а ты 3 раза отменял", "Ждала 3 часа, а ты 2 раза отменял", True),
        ("Приду в 5, уйду в 7", "Приду в 7, уйду в 5", True),
        ("Ты опоздал 2 раза за 3 дня", "Ты опоздал 3 раза за 2 дня", True),
        ("Ты пришёл в 7, а ушёл в 9", "Ты пришёл в 9, а ушёл в 7", True),
        ("Обещал 2 часа, ждала 5 часов", "Обещал 5 часов, ждала 2 часа", True),
        ("уже третий раз за 2 недели", "за 2 недели уже третий раз", False),
        ("Ты опоздал в 19:30!", "Ты пришёл в 19.30, мне обидно.", False),
        ("в 19:00", "в 19 часов", False),
        # Раунд 5: за предлогом берётся слово перед ним — обмен ловится.
        ("Ты обещал в 20, а пришёл в 23", "Ты пришёл в 20, а обещал в 23", True),
        # Известный пропуск (docstring facts.py): у обоих чисел соседи с одной основой.
        ("Он сказал 5, она сказала 7", "Он сказал 7, она сказала 5", False),
    ],
)
def test_facts_number_word_pairs(original: str, adapted: str, differ: bool) -> None:
    assert facts.numbers_differ(original, adapted) is differ


# ------------------------------------------------------------------ R3-MINOR-2, О-13


def test_default_v0_records_block_versions() -> None:
    r = crisis.respond(["self_harm"], withheld=True)
    assert r.template_version == "v0+not_sent@v1"
    assert crisis.respond(["self_harm"]).template_version == "v0"
    assert crisis.respond(["self_harm"], withheld=True, prompt_set=prompts.V2).template_version == (
        "v2"
    )
    assert crisis.LATE_BLOCKS["not_sent"] == "v1" and crisis.LATE_BLOCKS["author_safety"] == "v2"


def test_crisis_import_is_lazy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for f in (REPO_DIR / "prompts").glob("*.v0.md"):
        shutil.copy(f, tmp_path / f.name)
    monkeypatch.setenv(prompts.PROMPTS_DIR_ENV, str(tmp_path))
    prompts._read.cache_clear()
    try:
        module = importlib.reload(crisis)  # без crisis.v1/v2 импорт не падает
        assert module.respond(["self_harm"]).text == prompts.load("crisis", "v0", "self_harm").text
        with pytest.raises(prompts.PromptError):
            _ = module.NOT_SENT
    finally:
        monkeypatch.delenv(prompts.PROMPTS_DIR_ENV)
        prompts._read.cache_clear()
        importlib.reload(crisis)


# -------------------------------------------------------------------- A8-8, A8-9


def test_hint_guard_strips_stress_marks() -> None:
    original = "Агент, переда́й ему́ что́ я пода́ю на разво́д"
    assert pipeline.hint_guard("Передай ему что я подаю на развод", original) == "guard"
    assert pipeline.hint_guard("Похоже, автору важно, чтобы его услышали.", original) is None


@pytest.mark.parametrize("exc", [LLMUnavailable("503"), LLMBadOutput("мусор")])
async def test_hint_dropped_warning_on_meaning_failure(exc: Exception) -> None:
    bad = [exc, exc] if isinstance(exc, LLMBadOutput) else [exc]
    delivery = await process(scripted(meaning=bad), text="Ты опять опоздал!")
    assert delivery.status == "original_with_warning" and delivery.reply_hint is None
    assert delivery.hint_dropped == "warning" and delivery.degraded


async def test_no_hint_no_reason_on_adapt_failure() -> None:
    delivery = await process(scripted(adapt=LLMUnavailable("503")), text="Ты опять опоздал!")
    assert delivery.degraded == "adapt:unavailable" and delivery.hint_dropped is None

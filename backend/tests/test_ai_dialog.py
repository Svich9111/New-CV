"""Агент владельца, промпты v0, прототип и адаптер набора (задача 0.5) — без сети.

- Промпты v0: файл на роль, заголовок с разделами ТЗ, принципы §2 и запреты §6.2 п.5 в тексте,
  кризисные шаблоны без «потерпи» и без номеров.
- Диалог: контекст — только данные владельца (§6.1, §9.2 п.1); черновик — отдельный вызов;
  кризис (агент или вторая линия) — шаблон, без черновика; отказ модели (АР-20).
- Изоляция по всему набору: ``partner_private_context`` не уходит в модель; агент получателя —
  только оригинал.
- Прототип «Анна и Игорь»: без подтверждения ничего не доставляется, вывод без идентификаторов.
- ``compare.py --baseline`` и потолок расходов адаптера ``adapters.product``."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.ai import crisis, demo, dialog, prompts
from app.ai.dialog import OwnerContext, PairMessage, Turn
from app.ai.llm import LLMAuthError, LLMRefusal, LLMUnavailable
from app.ai.models import AgentModels
from app.ai.stub import ScriptedProvider, demo_handler

REPO_DIR = Path(__file__).resolve().parents[2]
EVALS_DIR = REPO_DIR / "evals"
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

from adapters import claude as claude_adapter  # noqa: E402 — путь к evals добавлен выше
from adapters import product  # noqa: E402


def _load(name: str, file: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, EVALS_DIR / file)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


run = _load("evals_run_ai_v0", "run.py")

CONTEXT = OwnerContext(
    owner_name="Анна",
    partner_name="Игорь",
    relationship="Вместе три года",
    memory=("Любит планировать вечера заранее",),
    knowledge=("Ненасильственное общение: чувство — потребность — просьба",),
    pair_messages=(PairMessage("incoming", "Задержусь сегодня."),),
)
TURNS = [Turn("owner", "Ему плевать на меня.")]


def answer(**kw: Any) -> dict[str, Any]:
    base = {
        "level": 2,
        "reply": "Слышу, как обидно. Что задело сильнее всего?",
        "draft_ready": False,
        "crisis_type": None,
        "crisis_secondary": [],
    }
    base.update(kw)
    return base


DRAFT = {
    "feeling": "Мне грустно",
    "situation": "планы отменяются",
    "need": "проводить время вместе",
    "request": "предупреждай заранее",
    "text": "Мне грустно, когда планы отменяются. Пожалуйста, предупреждай заранее.",
}
SAFE = {"category": "none", "secondary": []}


def provider(**answers: Any) -> ScriptedProvider:
    table = {"dialog": answer(), "draft": DRAFT, "safety": SAFE, **answers}

    def handler(role: str, payload: dict[str, Any], system: str) -> dict[str, Any]:
        value = table[role]
        if isinstance(value, Exception):
            raise value
        return dict(value)

    return ScriptedProvider(handler)


async def reply(p: ScriptedProvider, turns: list[Turn] | None = None) -> dialog.DialogResult:
    return await dialog.reply(AgentModels.single(p), CONTEXT, turns or TURNS)


# --------------------------------------------------------------------------- промпты v0


PRODUCT_ROLES = ("dialog", "draft", "safety", "tone", "adapt", "meaning")


@pytest.mark.parametrize("role", [*PRODUCT_ROLES, "crisis"])
def test_prompt_file_per_role_with_header(role: str) -> None:
    path = REPO_DIR / "prompts" / f"{role}.v0.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# ") and f"({role}.v0)" in text.splitlines()[0]
    assert "Разделы ТЗ" in text and "§" in text
    assert role in (REPO_DIR / "prompts" / "README.md").read_text(encoding="utf-8")


def test_dialog_prompt_has_principles_and_tone() -> None:
    text = prompts.system_prompt("dialog").text
    for phrase in (
        "переводчик, а не адвокат",
        "Слова остаются его",
        "Смягчать форму, не смысл",
        "Исповедь приватна",
        "Мост, а не замена",
        "Память под контролем",
        "не терапевт",
        "на «ты»",
        "без морализаторства",
        "сначала отрази чувство",
    ):
        assert phrase.lower() in text.lower(), phrase


# Запреты §6.2 п.5 — формулировками запрета, а не подстроками (ревью 0.5 MINOR-6а).
BANS = {
    "adapt": (
        "запрещено: менять, добавлять или убирать факты; убирать или менять просьбу; добавлять\n"
        "  просьбы, обещания, извинения",
    ),
    "draft": ("не придумывай фактов,\n  обещаний и извинений от его имени",),
}


@pytest.mark.parametrize("role", ["adapt", "draft"])
def test_adaptation_bans_from_tz_6_2_5(role: str) -> None:
    text = prompts.system_prompt(role).text.lower()
    for phrase in BANS[role]:
        assert phrase in text, phrase
    v1 = prompts.system_prompt("adapt", prompts.V2).text.lower()
    assert BANS["adapt"][0] in v1  # следующая версия запрет не теряет


def test_crisis_templates_without_softening_or_numbers() -> None:
    banned = ("потерп", "подбер", "все пройдет", "всё пройдёт", "успокой", "не зли", "послушн")
    for ctype in crisis.CRISIS_TYPES:
        text = prompts.load("crisis", "v0", block=ctype).text
        assert not re.search(r"\d", text.replace("18", "")), ctype  # номера — в «Помощь»
        assert "«Помощь»" in text or ctype == "outgoing_threat"
        assert not any(b in text.lower() for b in banned), ctype


def test_prompt_loader_errors_and_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(prompts.PromptError):
        prompts.load("unknown", "v0")
    with pytest.raises(prompts.PromptError):
        prompts.load("dialog", "0")
    with pytest.raises(prompts.PromptError):
        prompts.load("dialog", "v99")
    (tmp_path / "tone.v1.md").write_text("<!-- prompt:system -->\nX\n<!-- /prompt:system -->\n")
    monkeypatch.setenv(prompts.PROMPTS_DIR_ENV, str(tmp_path))
    assert prompts.system_prompt("tone", prompts.PromptSet(tone="v1")).text == "X"
    assert prompts.PromptSet(tone="v1").label().startswith("dialog.v0,")
    assert prompts.V0.label() == "v0"


# --------------------------------------------------------------------------- диалог


def test_owner_context_has_only_owner_fields() -> None:
    names = {f.name for f in dataclasses.fields(OwnerContext)}
    assert names == {
        "owner_name",
        "partner_name",
        "relationship",
        "memory",
        "knowledge",
        "pair_messages",
    }
    payload = dialog.context_payload(CONTEXT, TURNS)
    assert set(payload) == {
        "owner_name",
        "partner_name",
        "relationship",
        "owner_memory",
        "knowledge",
        "pair_messages",
        "dialog",
    }


async def test_clarifying_reply_without_draft() -> None:
    p = provider()
    result = await reply(p)
    assert [r["role"] for r in p.requests] == ["dialog", "safety"]
    assert result.draft is None and result.crisis is None
    assert (result.level, result.strategy) == (2, "find_need_prepare_message")
    assert result.prompts == ("dialog.v0", "safety.v0")
    # Вторая линия безопасности видит только реплики владельца.
    safety_payload = json.loads(p.requests[1]["messages"][-1][1])
    assert safety_payload == {"kind": "dialog", "text": "Ему плевать на меня."}


async def test_draft_is_a_separate_call_and_not_sent() -> None:
    p = provider(dialog=answer(draft_ready=True))
    result = await reply(p)
    assert [r["role"] for r in p.requests] == ["dialog", "safety", "draft"]
    assert result.draft is not None and result.draft.text == DRAFT["text"]
    assert result.draft.prompt == "draft.v0"
    draft_payload = json.loads(p.requests[2]["messages"][-1][1])
    assert draft_payload["revision"] is None


async def test_draft_revision() -> None:
    p = provider()
    revision = dialog.Revision(previous="Старый вариант", wish="Короче")
    result = await dialog.draft(AgentModels.single(p), CONTEXT, TURNS, revision=revision)
    assert result.text == DRAFT["text"]
    payload = json.loads(p.requests[0]["messages"][-1][1])
    assert payload["revision"] == {"previous": "Старый вариант", "wish": "Короче"}


@pytest.mark.parametrize("level", [1, 2, 3, 4])
async def test_strategy_follows_level(level: int) -> None:
    result = await reply(provider(dialog=answer(level=level)))
    assert result.strategy == dialog.STRATEGIES[level]


async def test_level_5_without_crisis_type_is_not_crisis() -> None:
    result = await reply(provider(dialog=answer(level=5)))
    assert result.level == 4 and result.crisis is None


async def test_agent_crisis_adds_template_and_blocks_draft() -> None:
    p = provider(
        dialog=answer(
            level=5,
            reply="Мне очень жаль, что с тобой это происходит.",
            draft_ready=True,
            crisis_type="violence",
        )
    )
    result = await reply(p)
    assert "draft" not in [r["role"] for r in p.requests]
    assert result.level == 5 and result.strategy == "crisis_protocol"
    assert result.draft is None and result.crisis is not None
    assert result.reply.startswith("Мне очень жаль")
    assert prompts.load("crisis", "v0", block="violence").text in result.reply
    assert {"name_it", "help_contacts", "no_draft", "no_softening", "no_tolerate"} <= (
        result.crisis.actions
    )
    assert "crisis.v0" in result.prompts


async def test_classifier_crisis_replaces_agent_reply() -> None:
    p = provider(
        dialog=answer(reply="Давай подберём слова, чтобы он не злился.", draft_ready=True),
        safety={"category": "self_harm", "secondary": []},
    )
    result = await reply(p)
    assert result.crisis is not None and result.crisis.primary == "self_harm"
    assert "подбер" not in result.reply.lower()
    assert result.reply == prompts.load("crisis", "v0", block="self_harm").text
    assert result.draft is None


async def test_venting_in_private_chat_is_not_crisis() -> None:
    result = await reply(provider(safety={"category": "threat_or_insult", "secondary": []}))
    assert result.crisis is None


async def test_underage_is_primary_and_refusal_goes_last() -> None:
    p = provider(
        dialog=answer(
            level=5,
            reply="Спасибо, что рассказала. То, что он делает, — это насилие.",
            crisis_type="violence",
            crisis_secondary=["self_harm"],
        ),
        safety={"category": "underage", "secondary": ["violence"]},
    )
    result = await reply(p)
    assert result.crisis is not None
    assert result.crisis.primary == "underage"
    assert set(result.crisis.secondary) == {"violence", "self_harm"}
    assert result.reply.startswith("Спасибо, что рассказала.")  # не начинаем с отказа
    assert result.reply.endswith(prompts.load("crisis", "v0", block="underage").text)


async def test_refusal_with_crisis_signs_takes_crisis_path() -> None:
    """АР-20: отказ модели — кризисный путь, если вторая линия нашла кризис."""
    p = provider(dialog=LLMRefusal("отказ"), safety={"category": "life_threat", "secondary": []})
    result = await reply(p)
    assert result.crisis is not None and result.crisis.primary == "life_threat"
    assert result.reply == prompts.load("crisis", "v0", block="life_threat").text


async def test_refusal_without_crisis_is_error_with_clear_message() -> None:
    with pytest.raises(LLMRefusal) as info:
        await reply(provider(dialog=LLMRefusal("отказ")))
    assert "«Помощь»" in dialog.error_reply(info.value)
    assert dialog.error_reply(LLMUnavailable("503")) == LLMUnavailable.user_message


async def test_second_line_failure_does_not_block_reply() -> None:
    result = await reply(provider(safety=LLMUnavailable("503")))
    assert result.safety_error == "unavailable" and result.reply


async def test_draft_failure_keeps_reply() -> None:
    result = await reply(provider(dialog=answer(draft_ready=True), draft=LLMUnavailable("503")))
    assert result.draft is None and result.draft_error == "unavailable" and result.reply


@pytest.mark.parametrize(
    "bad",
    [
        answer(level=0),
        answer(reply=""),
        answer(draft_ready="yes"),
        answer(crisis_type="panic"),
        {"level": 2, "reply": "x"},
    ],
)
async def test_dialog_json_errors(bad: dict[str, Any]) -> None:
    from app.ai.llm import LLMBadOutput

    with pytest.raises(LLMBadOutput):
        await reply(provider(dialog=bad))


async def test_dialog_must_end_with_owner_turn() -> None:
    with pytest.raises(ValueError):
        await reply(provider(), [Turn("owner", "Привет"), Turn("agent", "Привет!")])


# --------------------------------------------------------------------------- изоляция по набору


@pytest.fixture(scope="module")
def situations() -> list[dict[str, Any]]:
    return list(run.load_situations())


def stub_adapter() -> tuple[product.V0, ScriptedProvider]:
    p = ScriptedProvider(demo_handler)
    return product.V0(writer=p, classifier=p), p


def test_partner_private_context_never_reaches_model(situations: list[dict[str, Any]]) -> None:
    model, p = stub_adapter()
    report = run.build_report(situations, model, "isolation")
    assert not report["summary"]["model_errors"]
    sent = json.dumps(p.requests, ensure_ascii=False)
    secrets = [s for sit in situations for s in sit.get("partner_private_context", [])]
    assert secrets
    for secret in secrets:
        assert secret not in sent


def test_recipient_requests_contain_only_original(situations: list[dict[str, Any]]) -> None:
    model, p = stub_adapter()
    for sit in situations:
        pm = sit.get("partner_message")
        if not pm:
            continue
        p.requests.clear()
        model.deliver(run.DeliveryRequest(original=pm["original"]))
        for request in p.requests:
            payload = json.loads(request["messages"][-1][1])
            assert len(request["messages"]) == 1
            assert set(payload) in ({"kind", "text"}, {"original"}, {"original", "adapted"})
            assert payload.get("original", payload.get("text")) == pm["original"]
            body = json.dumps(request, ensure_ascii=False)
            for item in (*sit["owner_turns"], *sit.get("owner_memory", []), sit["owner"]["name"]):
                if item not in pm["original"]:
                    assert item not in body


def test_adapter_maps_withheld_outgoing_to_author_crisis(situations: list[dict[str, Any]]) -> None:
    model, _ = stub_adapter()
    sit = next(s for s in situations if s["id"] == "l5-03-outgoing-threat")
    resp = model.respond(run.build_request(sit))
    assert resp.crisis_type == "outgoing_threat" and resp.level == 5
    assert {"not_sent", "no_softening", "explain_reason", "return_to_talk"} <= resp.crisis_actions
    assert resp.draft is None and resp.safety_event and not resp.partner_notified
    delivery = model.deliver(run.DeliveryRequest(original=sit["partner_message"]["original"]))
    assert delivery.delivery == "withheld" and delivery.adapted is None
    metrics = model.pop_metrics()
    assert metrics["prompt"] == "product.v0"
    assert all(c["error"] is None for c in metrics["calls"])


def test_adapter_budget_stops_before_call() -> None:
    model, p = stub_adapter()
    model.budget = claude_adapter.CostBudget(0.1, first_call_reserve_usd=0.5)
    request = run.EvalRequest("Анна", "Игорь", "", (), ("Привет",), None)
    with pytest.raises(claude_adapter.BudgetExceeded):
        model.respond(request)
    assert p.requests == []


def test_adapter_error_kinds() -> None:
    def failing(exc: Exception) -> product.V0:
        def handler(role: str, payload: dict[str, Any], system: str) -> dict[str, Any]:
            raise exc

        p = ScriptedProvider(handler)
        return product.V0(writer=p, classifier=p)

    request = run.EvalRequest("Анна", "Игорь", "", (), ("Привет",), None)
    with pytest.raises(claude_adapter.RunAborted):
        failing(LLMAuthError("401")).respond(request)
    with pytest.raises(claude_adapter.ModelCallFailed):
        failing(LLMUnavailable("503")).respond(request)
    model = failing(LLMUnavailable("503"))
    with pytest.raises(claude_adapter.ModelCallFailed):
        model.respond(request)
    assert model.pop_metrics()["errors"] == ["unavailable", "unavailable"]


def test_adapter_requires_key_without_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ai.llm import LLMConfigError

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(LLMConfigError):
        product.V0()


def test_compare_baseline_option(tmp_path: Path, situations: list[dict[str, Any]]) -> None:
    compare = _load("evals_compare_ai_v0", "compare.py")
    base = run.build_report(situations, run.StubModel(), "base")
    (tmp_path / "base.json").write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    code = compare.main(
        [
            "--models",
            f"{__name__}:StubProduct",
            "--out-dir",
            str(tmp_path),
            "--label",
            "cmp",
            "--baseline",
            str(tmp_path / "base.json"),
        ]
    )
    assert code == 0
    summary = json.loads((tmp_path / "cmp.json").read_text(encoding="utf-8"))
    row = summary["rows"][0]
    assert row["comparison"]["baseline"] == "base"
    assert row["comparison"]["verdict"] in ("хуже", "не хуже")
    md = (tmp_path / "cmp.md").read_text(encoding="utf-8")
    assert "Сравнение `product.v0` с `base`" in md
    assert "Продуктовый конвейер" in md
    report = json.loads((tmp_path / "cmp_product.v0.json").read_text(encoding="utf-8"))
    assert report["comparison"]["baseline_model"] == "stub"
    assert compare.main(["--models", "stub", "--baseline", str(tmp_path / "nope.json")]) == 1


class StubProduct(product.V0):
    def __init__(self) -> None:
        p = ScriptedProvider(demo_handler)
        super().__init__(writer=p, classifier=p)


# --------------------------------------------------------------------------- прототип


IDS = re.compile(r"\b\d{5,}\b|[0-9a-f]{8}-[0-9a-f]{4}-|telegram", re.IGNORECASE)


def test_demo_scenario_delivers_only_after_confirmation(capsys: pytest.CaptureFixture[str]) -> None:
    assert demo.main(["--offline", "--send"]) == 0
    out = capsys.readouterr().out
    assert "[адаптировано]" in out
    assert "Показать оригинал" in out and demo.ANNA_VERSION in out
    assert "Экран Игоря" in out
    assert not IDS.search(out)


def test_demo_without_confirmation_sends_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert demo.main(["--offline"]) == 0
    out = capsys.readouterr().out
    assert "Не отправлено" in out
    assert "Экран Игоря" not in out


async def test_demo_declined_send_runs_no_pipeline() -> None:
    p = ScriptedProvider(demo_handler)
    lines: list[str] = []
    outcome = await demo.scenario(AgentModels.single(p), confirm=lambda _t: False, out=lines.append)
    assert not outcome.sent and outcome.delivery is None
    # Только диалог Анны: ни одного вызова агента получателя.
    assert {r["role"] for r in p.requests} <= {"dialog", "safety", "draft"}
    assert all(json.loads(r["messages"][-1][1]).get("kind") != "message" for r in p.requests)


def test_demo_without_key_is_clear_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert demo.main(["--send"]) == 3
    assert "config" in capsys.readouterr().err

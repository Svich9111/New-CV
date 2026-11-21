"""Поставщик OpenRouter (задача 0.4b, ТЗ §10, §9.1 п.4, §6.2; АР-20, АР-22) — без сети.

Запросы перехватывает ``httpx.MockTransport``; ключ — фиктивный (переменная окружения).
- Тело запроса: нет идентификаторов, ``data_collection: deny``, строгая json_schema.
- Агент получателя получает только оригинал (модель-«эхо»), ``converse`` — без оригинала.
- Ошибки OpenRouter → типизированные ``LLMError``; 402 и отказ — без резервной модели.
- Стоимость — ``usage.cost``; без неё — по прайсу; потолок расходов compare.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from app.ai.llm import (
    ChatMessage,
    FallbackProvider,
    LLMAuthError,
    LLMBadOutput,
    LLMConfigError,
    LLMError,
    LLMModelNotFound,
    LLMQuotaError,
    LLMRefusal,
    LLMRequestError,
    LLMUnavailable,
    OpenRouterProvider,
    Price,
    fetch_price,
)

REPO_DIR = Path(__file__).resolve().parents[2]
EVALS_DIR = REPO_DIR / "evals"
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

from adapters import claude as claude_adapter  # noqa: E402 — путь к evals добавлен выше
from adapters import openrouter as adapter  # noqa: E402

MODEL = "anthropic/claude-haiku-5.5"
SPARE = "openai/gpt-5.6-terra"
FAKE_KEY = "or-test-key-not-real"
SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}

Handler = Callable[[httpx.Request], httpx.Response]


def _load(name: str, file: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, EVALS_DIR / file)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


run = _load("evals_run_openrouter", "run.py")


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)


def client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def completion(
    content: str | None,
    *,
    model: str = MODEL,
    finish: str = "stop",
    native: str = "end_turn",
    refusal: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "gen-1",
        "object": "chat.completion",
        "model": model,
        "provider": "Test",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "native_finish_reason": native,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }
        ],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "cost": 0.0123}
        if usage is None
        else usage,
    }


def ok(data: dict[str, Any], **kw: Any) -> httpx.Response:
    return httpx.Response(200, json=completion(json.dumps(data, ensure_ascii=False), **kw))


def provider(handler: Handler, model: str = MODEL, **kw: Any) -> OpenRouterProvider:
    return OpenRouterProvider(model, client=client(handler), retry_delay_s=0, **kw)


async def ask(p: Any) -> Any:
    return await p.complete_json(
        system="Системный промпт", messages=[ChatMessage("user", "Привет")], schema=SCHEMA
    )


# --------------------------------------------------------------------------- тело запроса


async def test_request_body_has_no_ids_and_denies_data_collection() -> None:
    """§9.1 п.4: без идентификаторов; поставщик не хранит данные; строгая схема ответа."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok({"ok": True})

    result = await ask(provider(handler))
    assert result.data == {"ok": True}
    (request,) = seen
    assert request.url == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    for header in ("http-referer", "x-title"):
        assert header not in request.headers
    body = json.loads(request.content)
    assert set(body) == {
        "model",
        "messages",
        "max_tokens",
        "provider",
        "usage",
        "response_format",
    }
    assert body["model"] == MODEL
    assert body["provider"] == {"data_collection": "deny", "require_parameters": True}
    assert body["usage"] == {"include": True}
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "response", "strict": True, "schema": SCHEMA},
    }
    assert body["messages"] == [
        {"role": "system", "content": "Системный промпт"},
        {"role": "user", "content": "Привет"},
    ]
    for banned in ("user", "metadata", "temperature", "top_p", "reasoning", "session_id"):
        assert banned not in body


def test_no_key_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(LLMConfigError, match="OPENROUTER_API_KEY"):
        OpenRouterProvider(MODEL)
    with pytest.raises(LLMConfigError):
        adapter.OpenRouterAdapter(MODEL)  # до запроса каталога


# --------------------------------------------------------------------------- стоимость


async def test_cost_from_usage_cost_and_tokens() -> None:
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "cost": 0.0042,
        "prompt_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 0},
    }
    result = await ask(provider(lambda r: ok({"ok": True}, usage=usage)))
    assert result.cost_usd == pytest.approx(0.0042)
    assert result.usage.input_tokens == 800 and result.usage.cache_read_input_tokens == 200
    assert result.usage.output_tokens == 100


async def test_cost_falls_back_to_price_when_usage_has_no_cost() -> None:
    usage = {"prompt_tokens": 1000, "completion_tokens": 100}
    p = provider(lambda r: ok({"ok": True}, usage=usage), price=Price(2.0, 10.0))
    result = await ask(p)
    assert result.cost_usd == pytest.approx((1000 * 2 + 100 * 10) / 1e6)


async def test_no_cost_and_no_price_is_config_error() -> None:
    """Расход не считается как $0 (ТЗ §10)."""
    usage = {"prompt_tokens": 1000, "completion_tokens": 100}
    with pytest.raises(LLMConfigError, match=r"usage\.cost"):
        await ask(provider(lambda r: ok({"ok": True}, usage=usage)))


async def test_fetch_price_from_catalog() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://openrouter.ai/api/v1/models"
        rows = [{"id": MODEL, "pricing": {"prompt": "0.0000001", "completion": "0.0000005"}}]
        return httpx.Response(200, json={"data": rows})

    price = await fetch_price(MODEL, client(handler))
    assert price.input_per_mtok == pytest.approx(0.1)
    assert price.output_per_mtok == pytest.approx(0.5)
    with pytest.raises(LLMModelNotFound):
        await fetch_price("nobody/none", client(handler))


# --------------------------------------------------------------------------- ошибки


def _error(status: int, message: str = "x") -> Handler:
    body = {"error": {"message": message, "code": status}, "user_id": "user_secret_id"}
    return lambda r: httpx.Response(status, json=body)


@pytest.mark.parametrize(
    ("status", "error", "fallback"),
    [
        (401, LLMAuthError, False),
        (403, LLMAuthError, False),
        (402, LLMQuotaError, False),
        (400, LLMRequestError, False),
        (404, LLMModelNotFound, True),
        (408, LLMUnavailable, True),
        (429, LLMUnavailable, True),
        (500, LLMUnavailable, True),
        (502, LLMUnavailable, True),
        (503, LLMUnavailable, True),
    ],
)
async def test_status_errors_are_typed(status: int, error: type, fallback: bool) -> None:
    with pytest.raises(error) as info:
        await ask(provider(_error(status), retries=0))
    assert info.value.fallback_ok is fallback
    assert "user_secret_id" not in str(info.value)


async def test_error_inside_200_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"message": "limit", "code": 402}})

    with pytest.raises(LLMQuotaError):
        await ask(provider(handler))


async def test_network_error_is_unavailable_and_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("down", request=request)
        return ok({"ok": True})

    assert (await ask(provider(handler, retries=2))).data == {"ok": True}
    assert calls == 3
    calls = -100
    with pytest.raises(LLMUnavailable):
        await ask(provider(handler, retries=1))


async def test_timeout_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMUnavailable, match="тайм-аут"):
        await ask(provider(handler, retries=0))


async def test_quota_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _error(402)(request)

    with pytest.raises(LLMQuotaError):
        await ask(provider(handler, retries=2))
    assert calls == 1


@pytest.mark.parametrize(
    "kw",
    [
        {"refusal": "I can't help with that."},
        {"finish": "content_filter"},
        {"native": "refusal"},
    ],
)
async def test_refusal_is_typed_and_paid(kw: dict[str, Any]) -> None:
    with pytest.raises(LLMRefusal) as info:
        await ask(provider(lambda r: httpx.Response(200, json=completion(None, **kw))))
    assert info.value.fallback_ok is False
    assert info.value.cost_usd == pytest.approx(0.0123)


@pytest.mark.parametrize(
    ("content", "kw", "match"),
    [
        ('{"ok": tr', {"finish": "length"}, "обрезан"),
        (None, {}, "пустой"),
        ("   ", {}, "пустой"),
        ("не json", {}, "JSON"),
        ("[1, 2]", {}, "объект"),
    ],
)
async def test_bad_output_is_typed_and_paid(
    content: str | None, kw: dict[str, Any], match: str
) -> None:
    with pytest.raises(LLMBadOutput, match=match) as info:
        await ask(provider(lambda r: httpx.Response(200, json=completion(content, **kw))))
    assert info.value.cost_usd == pytest.approx(0.0123)


async def test_no_choices_and_non_json_body() -> None:
    with pytest.raises(LLMBadOutput):
        await ask(provider(lambda r: httpx.Response(200, json={"choices": []})))
    with pytest.raises(LLMBadOutput):
        await ask(provider(lambda r: httpx.Response(200, text="<html>")))


async def test_fallback_on_unavailable_but_not_on_refusal_or_quota() -> None:
    """АР-20: отказ — не на резервную модель; 402 — остановка; 503 — резервная."""
    spare_calls: list[str] = []

    def spare(request: httpx.Request) -> httpx.Response:
        spare_calls.append(json.loads(request.content)["model"])
        return ok({"ok": True}, model=SPARE)

    chain = FallbackProvider(
        provider(_error(503), retries=0), provider(spare, model=SPARE, retries=0)
    )
    result = await ask(chain)
    assert result.model == SPARE and result.fallback_used and spare_calls == [SPARE]

    for primary in (
        provider(lambda r: httpx.Response(200, json=completion(None, refusal="no"))),
        provider(_error(402)),
    ):
        spare_calls.clear()
        with pytest.raises((LLMRefusal, LLMQuotaError)):
            await ask(FallbackProvider(primary, provider(spare, model=SPARE)))
        assert spare_calls == []


# --------------------------------------------------------------------------- адаптер evals

OWNER = {
    "level": 2,
    "strategy": "find_need_prepare_message",
    "reply": "Слышу тебя. Что задело сильнее всего?",
    "draft": "Мне грустно. Давай обсудим?",
    "crisis_type": None,
    "crisis_actions": [],
}


def _is_recipient(body: dict[str, Any]) -> bool:
    schema = body["response_format"]["json_schema"]["schema"]
    return "safety" in schema["properties"]


def echo(bodies: list[dict[str, Any]], cost: float = 0.001) -> Handler:
    """Модель-«эхо»: агент получателя возвращает в adapted ровно то, что получил."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        usage = {"prompt_tokens": 100, "completion_tokens": 10, "cost": cost}
        if _is_recipient(body):
            data = {
                "safety": "none",
                "tone": 3,
                "adapted": body["messages"][1]["content"],
                "reply_hint": "Ответь мягко.",
            }
            return ok(data, usage=usage)
        return ok(OWNER, usage=usage)

    return handler


def _adapter(handler: Handler, model: str = MODEL) -> adapter.OpenRouterAdapter:
    return adapter.OpenRouterAdapter(model, provider=provider(handler, model=model))


def test_factory_by_model_id() -> None:
    factory = run.load_model.__globals__["importlib"].import_module("adapters.openrouter")
    made = getattr(factory, "deepseek/deepseek-v4-pro")
    assert callable(made)
    with pytest.raises(AttributeError):
        factory.NoSuchClass  # noqa: B018


def test_recipient_gets_only_original_and_converse_no_original() -> None:
    """ТЗ §6.2 (последний абзац), §9.2 п.1: вызов доставки — только оригинал; шаги диалога —
    без одобренного оригинала (его видит только deliver)."""
    situations = run.load_situations()
    bodies: list[dict[str, Any]] = []
    model = _adapter(echo(bodies))
    report = run.build_report(situations, model, "echo")
    model.close()

    with_message = [s for s in situations if "partner_message" in s]
    recipient = [b for b in bodies if _is_recipient(b)]
    assert len(recipient) == len(with_message)
    for s, body in zip(with_message, recipient, strict=True):
        assert body["provider"]["data_collection"] == "deny"
        assert len(body["messages"]) == 2  # системный промпт и один вход
        assert body["messages"][0]["role"] == "system"
        payload = json.loads(body["messages"][1]["content"])
        assert payload == {"original": s["partner_message"]["original"]}
        text = body["messages"][1]["content"]
        for entry in [*s["owner_turns"], *s.get("owner_memory", [])]:
            if entry not in s["partner_message"]["original"]:
                assert entry not in text
        for name in (s["owner"]["name"], s["owner"]["partner_name"]):
            if name not in s["partner_message"]["original"]:
                assert name not in text

    dialog = [s for s in situations if s.get("dialog") and "partner_message" in s]
    owner_bodies = "\n".join(
        m["content"] for b in bodies if not _is_recipient(b) for m in b["messages"]
    )
    for s in dialog:
        assert s["partner_message"]["original"] not in owner_bodies, s["id"]

    rows = {r["id"]: r for r in report["results"]}
    for s in with_message:
        assert rows[s["id"]]["response"]["adapted"] is not None
    assert report["metrics"]["cost_usd_total"] == pytest.approx(len(bodies) * 0.001)


def test_refusal_in_adapter_fails_situation_and_counts_cost() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=completion(None, refusal="no"))

    situations = run.load_situations()[:2]
    model = _adapter(handler)
    report = run.build_report(situations, model, "refuse")
    model.close()
    assert report["summary"]["model_errors"] == [s["id"] for s in situations]
    assert all("refusal" in r["metrics"]["errors"] for r in report["results"])
    assert report["metrics"]["cost_usd_total"] > 0


def test_bad_request_aborts_only_this_model() -> None:
    model = _adapter(_error(400, "schema not supported"))
    with pytest.raises(claude_adapter.RunAborted) as info:
        run.build_report(run.load_situations()[:1], model, "bad")
    model.close()
    assert info.value.skip_model is True and info.value.kind == "bad_request"


def test_quota_aborts_run() -> None:
    model = _adapter(_error(402))
    with pytest.raises(claude_adapter.RunAborted) as info:
        run.build_report(run.load_situations()[:1], model, "quota")
    model.close()
    assert info.value.skip_model is False and info.value.kind == "quota"


# --------------------------------------------------------------------------- потолок расходов


def test_budget_stops_before_exceeding() -> None:
    budget = claude_adapter.CostBudget(1.0, first_call_reserve_usd=0.3)
    budget.check("m")
    budget.add("m", 0.4)
    budget.check("m")  # 0.4 + 0.4 <= 1.0
    budget.add("m", 0.4)
    with pytest.raises(claude_adapter.BudgetExceeded):
        budget.check("m")  # 0.8 + 0.4 > 1.0
    assert budget.spent_usd == pytest.approx(0.8)


class EchoCheap(adapter.OpenRouterAdapter):
    def __init__(self) -> None:
        super().__init__(MODEL, provider=provider(echo([], cost=0.01)))


class EchoPricey(adapter.OpenRouterAdapter):
    def __init__(self) -> None:
        super().__init__(SPARE, provider=provider(echo([], cost=0.05), model=SPARE))


class BadSchema(adapter.OpenRouterAdapter):
    def __init__(self) -> None:
        super().__init__("google/x", provider=provider(_error(400), model="google/x"))


def test_compare_budget_keeps_done_reports_and_exits_nonzero(tmp_path: Path) -> None:
    """Потолок: первая модель прогнана целиком (отчёт сохранён), вторая остановлена до
    превышения; сводка написана, код выхода 5, потрачено не больше потолка."""
    compare = _load("evals_compare_openrouter", "compare.py")
    situations = run.load_situations()
    cheap_calls = len(situations) * 2  # заведомо больше числа вызовов первой модели
    limit = cheap_calls * 0.01 + 0.5 + 0.2  # хватает на первую, но не на всю вторую
    code = compare.main(
        [
            "--models",
            f"{__name__}:EchoCheap",
            f"{__name__}:EchoPricey",
            "--out-dir",
            str(tmp_path),
            "--label",
            "cap",
            "--max-cost-usd",
            str(limit),
        ]
    )
    assert code == 5
    summary = json.loads((tmp_path / "cap.json").read_text(encoding="utf-8"))
    assert [r["model"] for r in summary["rows"]] == [MODEL]
    assert summary["budget_stopped"] is True and summary["not_run"] == [SPARE]
    assert summary["spent_usd"] <= limit
    assert (tmp_path / f"cap_{run._safe_label(MODEL)}.json").exists()
    assert not (tmp_path / f"cap_{run._safe_label(SPARE)}.json").exists()
    md = (tmp_path / "cap.md").read_text(encoding="utf-8")
    assert "потолком расходов" in md and "$ всего" in md and "Не проверено" in md
    assert f"Потрачено всего (включая прерванную модель): ${summary['spent_usd']:.4f}" in md
    assert summary["spent_usd"] > sum(r["cost_usd_total"] for r in summary["rows"])


def test_compare_skips_model_with_bad_request(tmp_path: Path) -> None:
    compare = _load("evals_compare_openrouter2", "compare.py")
    code = compare.main(
        [
            "--models",
            f"{__name__}:BadSchema",
            f"{__name__}:EchoCheap",
            "--out-dir",
            str(tmp_path),
            "--label",
            "skip",
        ]
    )
    assert code == 3
    summary = json.loads((tmp_path / "skip.json").read_text(encoding="utf-8"))
    assert [r["model"] for r in summary["rows"]] == [MODEL]
    assert "google/x" in summary["skipped"] and summary["not_answered"] == ["google/x"]
    assert "Модель пропущена" in (tmp_path / "skip.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- раунд 1 ревью 0.4b


def test_budget_reserve_is_price_ceiling_not_only_observed() -> None:
    """MINOR-1: следующий вызов может быть дороже наблюдённого — резерв по прайсу останавливает
    прогон до него."""
    budget = claude_adapter.CostBudget(1.0)
    budget.add("m", 0.05)  # наблюдённый максимум — 0.05
    budget.check("m")  # без прайса: 0.05 + 0.05 <= 1.0
    budget.check("m", ceiling_usd=0.9)  # 0.05 + 0.9 <= 1.0
    with pytest.raises(claude_adapter.BudgetExceeded):
        budget.check("m", ceiling_usd=0.96)  # следующий вызов может стоить дороже наблюдённого
    assert budget.reserve("m", 0.01) == pytest.approx(0.05)  # наблюдённый больше оценки


def test_adapter_stops_before_call_that_could_exceed_limit() -> None:
    """MINOR-1: первые вызовы дешёвые (0.001), но верхняя граница вызова по прайсу —
    16000 × $10/млн = $0.16: при потолке $0.15 вызов не делается вовсе."""
    calls: list[dict[str, Any]] = []
    model = adapter.OpenRouterAdapter(MODEL, provider=provider(echo(calls), price=Price(1.0, 10.0)))
    model.budget = claude_adapter.CostBudget(0.15)
    with pytest.raises(claude_adapter.BudgetExceeded):
        run.build_report(run.load_situations()[:1], model, "cap")
    model.close()
    assert calls == []
    ceiling = model._call_ceiling("x" * 1000, [ChatMessage("user", "y" * 1000)])
    assert ceiling == pytest.approx((6000 * 1.0 + 16000 * 10.0) / 1e6)


def test_ceiling_unknown_without_price() -> None:
    model = _adapter(echo([]))
    assert model._call_ceiling("x", []) is None
    model.close()


async def test_fetch_price_without_price_is_config_error() -> None:
    """MINOR-4: нет цены в каталоге — ошибка настройки, а не $0."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": MODEL, "pricing": {"prompt": "1e-7"}}]})

    with pytest.raises(LLMConfigError, match="нет цены"):
        await fetch_price(MODEL, client(handler))


async def test_cache_multipliers_only_for_anthropic() -> None:
    """MINOR-4: множители кэша Anthropic (×0,1 чтение) — только для anthropic/*; у других —
    по полной цене входа (оценка сверху)."""
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 0,
        "prompt_tokens_details": {"cached_tokens": 1000},
    }
    price = Price(1.0, 1.0)
    anth = await ask(provider(lambda r: ok({"ok": True}, usage=usage), price=price))
    other = await ask(provider(lambda r: ok({"ok": True}, usage=usage), model=SPARE, price=price))
    assert anth.cost_usd == pytest.approx(1000 * 0.1 / 1e6)
    assert other.cost_usd == pytest.approx(1000 / 1e6)


async def test_original_error_kept_when_cost_unknown() -> None:
    """MINOR-5: отказ без usage.cost и без прайса — остаётся отказом, стоимость None."""
    usage = {"prompt_tokens": 10, "completion_tokens": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=completion(None, refusal="no", usage=usage))

    with pytest.raises(LLMRefusal) as info:
        await ask(provider(handler))
    assert info.value.cost_usd is None


async def _stream(p: OpenRouterProvider) -> list[str]:
    return [
        chunk async for chunk in p.stream_text(system="s", messages=[ChatMessage("user", "Привет")])
    ]


async def test_stream_text_checks_finish_reason_and_choices() -> None:
    """MINOR-6: stream_text разбирает ответ так же, как complete_json."""
    assert await _stream(provider(lambda r: httpx.Response(200, json=completion("Текст")))) == [
        "Текст"
    ]
    assert "response_format" not in provider(lambda r: ok({})).request_body("s", [], 10)
    for kw, error in (
        ({"finish": "length"}, LLMBadOutput),
        ({"finish": "content_filter"}, LLMRefusal),
        ({"native": "refusal"}, LLMRefusal),
    ):
        with pytest.raises(error):
            await _stream(
                provider(lambda r, kw=kw: httpx.Response(200, json=completion("x", **kw)))
            )
    for broken in ({"choices": []}, {"choices": ["x"]}, {"choices": [{"message": None}]}):
        with pytest.raises(LLMBadOutput):
            await _stream(provider(lambda r, b=broken: httpx.Response(200, json=b)))


@pytest.mark.parametrize("status", [400, 401, 402, 429, 500])
async def test_key_never_in_error_text(status: int) -> None:
    """Ключ не попадает в текст ошибки, даже если сервис вернул его в сообщении."""
    body = {"error": {"message": f"bad key {FAKE_KEY[:6]}", "code": status}}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(status, json=body)

    with pytest.raises(LLMError) as info:
        await ask(provider(handler, retries=0))
    assert seen == [f"Bearer {FAKE_KEY}"]
    assert FAKE_KEY not in str(info.value) and FAKE_KEY not in repr(info.value.__dict__)


async def test_key_never_in_network_error_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom {request.headers['authorization']}", request=request)

    with pytest.raises(LLMUnavailable) as info:
        await ask(provider(handler, retries=0))
    assert FAKE_KEY not in str(info.value)


# --------------------------------------------------------------------------- пересчёт (Б-35)


def test_rescore_uses_saved_answers_without_calls(tmp_path: Path) -> None:
    """Пересчёт сводки по сохранённым ответам: модели не вызываются, ответы и метрики те же,
    проверки — текущие; пометка о пересчёте в сводке и отчётах."""
    compare = _load("evals_compare_rescore", "compare.py")
    assert (
        compare.main(
            ["--models", f"{__name__}:EchoCheap", "--out-dir", str(tmp_path), "--label", "r"]
        )
        == 0
    )
    report_path = tmp_path / f"r_{run._safe_label(MODEL)}.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    row = next(x for x in report["results"] if x["id"] == "l5-02-control")
    row["response"]["reply"] = "Ты не обязан становиться «послушнее»."
    row["checks"]["must_not_contain"] = {"passed": False, "detail": "найдено: послушн"}
    bad = next(x for x in report["results"] if x["id"] == "l5-01-pushed-violence")
    bad["response"]["reply"] = "Нет тебе надо потерпеть."
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    metrics_before = report["metrics"]

    def no_calls(*a: Any, **kw: Any) -> Any:
        raise AssertionError("модель вызвана при пересчёте")

    original = OpenRouterProvider.complete_json
    OpenRouterProvider.complete_json = no_calls  # type: ignore[method-assign]
    try:
        assert compare.main(["--rescore", str(tmp_path / "r.json")]) == 0
    finally:
        OpenRouterProvider.complete_json = original  # type: ignore[method-assign]

    new = json.loads(report_path.read_text(encoding="utf-8"))
    rows = {x["id"]: x for x in new["results"]}
    assert rows["l5-02-control"]["checks"]["must_not_contain"]["passed"] is True
    assert rows["l5-01-pushed-violence"]["checks"]["must_not_contain"]["passed"] is False
    assert rows["l5-01-pushed-violence"]["auto_pass"] is False
    assert new["metrics"] == metrics_before and new["rescored"]["note"] == compare.RESCORE_NOTE
    summary = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert summary["rescored"] == compare.RESCORE_NOTE
    assert summary["rows"][0]["auto_pass_rate"] == new["summary"]["auto_pass_rate"]
    assert "Пересчитано после Б-35" in (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "Пересчитано после Б-35" in report_path.with_suffix(".md").read_text(encoding="utf-8")


def test_rescore_refuses_changed_dataset(tmp_path: Path) -> None:
    compare = _load("evals_compare_rescore2", "compare.py")
    compare.main(["--models", f"{__name__}:EchoCheap", "--out-dir", str(tmp_path), "--label", "q"])
    report_path = tmp_path / f"q_{run._safe_label(MODEL)}.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["dataset_hash"] = "0000"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    assert compare.main(["--rescore", str(tmp_path / "q.json")]) == 1

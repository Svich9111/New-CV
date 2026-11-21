"""A8: независимые проверки слоя над моделью, адаптера Claude и сравнения (задача 0.4).

Основания: ТЗ §6.1, §6.2 (последний абзац — агент получателя видит только оригинал), §9.1 п.4
(без идентификаторов Telegram), §9.2 п.1, §10 (слой абстракции, резервная модель, понятная
ошибка при отказе, контроль расходов); CLAUDE.md (ключи только в окружении, в журналах нет
текстов сообщений).

Без сети: SDK ``anthropic`` получает подменённый транспорт ``httpx2.MockTransport`` (в процессе)
или локальный HTTP-сервер на 127.0.0.1 (подпроцессы CLI). Хелперы исполнителя
(``test_llm_layer.py``) не используются. БД не нужна. Всё, что пишут прогоны, — в tmp_path.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
import traceback
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

import anthropic
import httpx2
import pytest

from app.ai.llm import (
    HAIKU,
    OPUS,
    SONNET,
    ChatMessage,
    ClaudeProvider,
    FallbackProvider,
    LLMAuthError,
    LLMBadOutput,
    LLMConfigError,
    LLMError,
    LLMRefusal,
    LLMRequestError,
    LLMUnavailable,
    Usage,
    cost_usd,
)

REPO_DIR = Path(__file__).resolve().parents[2]
EVALS_DIR = REPO_DIR / "evals"
BACKEND_DIR = REPO_DIR / "backend"
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

from adapters import claude as claude_adapter  # noqa: E402 — путь к evals добавлен выше

FAKE_KEY = "sk-ant-a8-FAKE-KEY-do-not-log-7f3c"


def _load_run() -> ModuleType:
    spec = importlib.util.spec_from_file_location("evals_run_llm_a8", EVALS_DIR / "run.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["evals_run_llm_a8"] = module
    spec.loader.exec_module(module)
    return module


run = _load_run()

Reply = Callable[[httpx2.Request], httpx2.Response]


class Wire:
    """Подменённый транспорт: запоминает каждый HTTP-запрос (тело и заголовки)."""

    def __init__(self, reply: Reply) -> None:
        self.reply = reply
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        self.headers.append({k.lower(): v for k, v in request.headers.items()})
        return self.reply(request)

    def client(self) -> anthropic.AsyncAnthropic:
        return anthropic.AsyncAnthropic(
            api_key=FAKE_KEY,
            base_url="http://a8-llm.invalid",
            max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self)),
        )


def _message(
    content: list[dict[str, Any]],
    *,
    model: str = OPUS,
    stop_reason: str | None = "end_turn",
    usage: dict[str, int] | None = None,
    stop_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "msg_a8",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": stop_details,
        "usage": usage or {"input_tokens": 10, "output_tokens": 5},
    }


def _json_reply(data: Any, **kw: Any) -> httpx2.Response:
    return httpx2.Response(200, json=_message([{"type": "text", "text": json.dumps(data)}], **kw))


def _error(status: int, kind: str = "api_error") -> httpx2.Response:
    return httpx2.Response(status, json={"type": "error", "error": {"type": kind, "message": "x"}})


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
TALK = [ChatMessage("user", "Мне обидно, что вечер снова отменился.")]


async def _ask(provider: Any) -> Any:
    return await provider.complete_json(system="Системный A8", messages=TALK, schema=SCHEMA)


# =========================================================================== форма запроса

ALLOWED_BODY_KEYS = {"model", "max_tokens", "system", "messages", "thinking", "output_config"}
SDK_HEADERS = {
    "host",
    "accept",
    "accept-encoding",
    "connection",
    "content-type",
    "content-length",
    "user-agent",
    "authorization",
    "x-api-key",
    "anthropic-version",
}


def _check_headers(headers: dict[str, str]) -> None:
    extra = {h for h in headers if h not in SDK_HEADERS and not h.startswith("x-stainless-")}
    assert extra == set(), extra  # нет своих заголовков с идентификаторами (§9.1 п.4)


@pytest.mark.parametrize("effort", [None, "low"])
async def test_request_body_no_sampling_no_ids_adaptive(effort: Any) -> None:
    """Тело запроса: только нужные поля; нет temperature/top_p/top_k, metadata/user_id;
    thinking — adaptive; схема — в output_config.format (ТЗ §9.1 п.4, §10)."""
    wire = Wire(lambda r: _json_reply({"ok": True}))
    result = await _ask(ClaudeProvider(SONNET, client=wire.client(), effort=effort))
    assert result.data == {"ok": True}
    (body,) = wire.bodies
    assert set(body) <= ALLOWED_BODY_KEYS, set(body) - ALLOWED_BODY_KEYS
    for banned in ("temperature", "top_p", "top_k", "metadata", "user", "user_id", "stream"):
        assert banned not in body
    assert body["thinking"] == {"type": "adaptive"}
    assert body["model"] == SONNET
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert body["output_config"].get("effort") == effort
    assert body["messages"] == [{"role": "user", "content": TALK[0].content}]
    _check_headers(wire.headers[0])


def _sse(events: list[dict[str, Any]]) -> httpx2.Response:
    raw = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return httpx2.Response(200, content=raw.encode(), headers={"content-type": "text/event-stream"})


def _stream(chunks: list[str], stop_reason: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": _message([], stop_reason=None)},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    ]
    for c in chunks:
        delta = {"type": "text_delta", "text": c}
        events.append({"type": "content_block_delta", "index": 0, "delta": delta})
    return [
        *events,
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 3},
        },
        {"type": "message_stop"},
    ]


async def test_stream_body_same_rules_and_refusal_is_typed() -> None:
    wire = Wire(lambda r: _sse(_stream(["Слышу ", "тебя"], "end_turn")))
    provider = ClaudeProvider(OPUS, client=wire.client())
    chunks = [c async for c in provider.stream_text(system="s", messages=TALK)]
    assert "".join(chunks) == "Слышу тебя"
    body = wire.bodies[0]
    assert body.get("stream") is True
    assert set(body) - {"stream"} <= ALLOWED_BODY_KEYS
    assert "temperature" not in body and "top_p" not in body and "metadata" not in body
    assert body["thinking"] == {"type": "adaptive"}
    assert "output_config" not in body

    refusing = ClaudeProvider(OPUS, client=Wire(lambda r: _sse(_stream([], "refusal"))).client())
    with pytest.raises(LLMRefusal):
        async for _ in refusing.stream_text(system="s", messages=TALK):
            pass


# =========================================================================== ответ → ошибка


@pytest.mark.parametrize(
    ("content", "stop_reason", "error"),
    [
        ([{"type": "text", "text": '{"ok": true}'}], "refusal", LLMRefusal),
        ([{"type": "text", "text": '{"ok": tr'}], "max_tokens", LLMBadOutput),
        ([{"type": "text", "text": '{"ok": true}'}], "max_tokens", LLMBadOutput),
        ([{"type": "text", "text": '{"ok": true}'}], "model_context_window_exceeded", LLMBadOutput),
        ([], "end_turn", LLMBadOutput),
        ([{"type": "text", "text": ""}], "end_turn", LLMBadOutput),
        ([{"type": "text", "text": "Конечно! Вот ответ."}], "end_turn", LLMBadOutput),
        ([{"type": "text", "text": "[1, 2]"}], "end_turn", LLMBadOutput),
        ([{"type": "text", "text": "null"}], "end_turn", LLMBadOutput),
    ],
    ids=[
        "refusal с валидным JSON",
        "обрезан",
        "max_tokens с валидным JSON",
        "окно контекста",
        "пустой content",
        "пустой текст",
        "не JSON",
        "массив",
        "null",
    ],
)
async def test_bad_response_is_typed_error_not_silent_result(
    content: list[dict[str, Any]], stop_reason: str, error: type[LLMError]
) -> None:
    """Отказ, обрезанный и пустой ответ — типизированная ошибка, а не «тихий» результат
    (ТЗ §10: понятное сообщение, черновик не теряется). stop_reason проверяется до content."""
    wire = Wire(lambda r: httpx2.Response(200, json=_message(content, stop_reason=stop_reason)))
    with pytest.raises(error) as info:
        await _ask(ClaudeProvider(OPUS, client=wire.client()))
    assert info.value.user_message  # есть понятный текст для пользователя
    assert info.value.model == OPUS
    if error is LLMBadOutput:
        assert info.value.fallback_ok


async def test_refusal_category_is_kept() -> None:
    reply = httpx2.Response(
        200,
        json=_message(
            [], stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber"}
        ),
    )
    with pytest.raises(LLMRefusal) as info:
        await _ask(ClaudeProvider(OPUS, client=Wire(lambda r: reply).client()))
    assert info.value.category == "cyber"


# =========================================================================== резервная модель


def _raise(exc: Exception) -> Reply:
    def reply(request: httpx2.Request) -> httpx2.Response:
        raise exc

    return reply


@pytest.mark.parametrize(
    "primary",
    [
        lambda r: _error(429, "rate_limit_error"),
        lambda r: _error(500),
        lambda r: _error(502),
        lambda r: _error(503),
        lambda r: _error(529, "overloaded_error"),
        lambda r: _error(404, "not_found_error"),
        _raise(httpx2.ConnectError("нет сети")),
        _raise(httpx2.ReadTimeout("тайм-аут")),
        lambda r: httpx2.Response(200, json=_message([], stop_reason="end_turn")),
    ],
    ids=["429", "500", "502", "503", "529", "404", "сеть", "тайм-аут", "пусто"],
)
async def test_unavailable_goes_to_fallback(primary: Reply) -> None:
    """ТЗ §10: «Предусмотрена резервная модель»: 429/5xx/сеть/тайм-аут → ответ резервной."""
    main, spare = Wire(primary), Wire(lambda r: _json_reply({"ok": True}, model=HAIKU))
    chain = FallbackProvider(
        ClaudeProvider(OPUS, client=main.client()), ClaudeProvider(HAIKU, client=spare.client())
    )
    result = await _ask(chain)
    assert result.data == {"ok": True}
    assert result.model == HAIKU and result.fallback_used
    assert result.attempts == (OPUS, HAIKU)
    assert len(main.bodies) == 1 and len(spare.bodies) == 1
    assert spare.bodies[0]["messages"] == main.bodies[0]["messages"]  # тот же запрос


@pytest.mark.parametrize(
    ("status", "kind", "error"),
    [
        (400, "invalid_request_error", LLMRequestError),
        (401, "authentication_error", LLMAuthError),
        (403, "permission_error", LLMAuthError),
        (413, "request_too_large", LLMRequestError),
    ],
)
async def test_config_errors_do_not_fall_back(
    status: int, kind: str, error: type[LLMError]
) -> None:
    main, spare = Wire(lambda r: _error(status, kind)), Wire(lambda r: _json_reply({"ok": True}))
    chain = FallbackProvider(
        ClaudeProvider(OPUS, client=main.client()), ClaudeProvider(HAIKU, client=spare.client())
    )
    with pytest.raises(error):
        await _ask(chain)
    assert spare.bodies == []


async def test_refusal_does_not_go_to_fallback() -> None:
    """DECISIONS.md АР-20: отказ модели (refusal) не переадресуется резервной модели."""
    refusal = httpx2.Response(200, json=_message([], stop_reason="refusal"))
    main, spare = Wire(lambda r: refusal), Wire(lambda r: _json_reply({"ok": True}, model=HAIKU))
    chain = FallbackProvider(
        ClaudeProvider(OPUS, client=main.client()), ClaudeProvider(HAIKU, client=spare.client())
    )
    with pytest.raises(LLMRefusal):
        await _ask(chain)
    assert spare.bodies == []


async def test_all_down_gives_unavailable_with_user_message() -> None:
    chain = FallbackProvider(
        ClaudeProvider(OPUS, client=Wire(lambda r: _error(503)).client()),
        ClaudeProvider(SONNET, client=Wire(lambda r: _error(529, "overloaded_error")).client()),
    )
    with pytest.raises(LLMUnavailable) as info:
        await _ask(chain)
    assert "сохранён" in info.value.user_message  # ТЗ §10: «черновик не теряется»


async def test_stream_falls_back_before_first_chunk() -> None:
    chain = FallbackProvider(
        ClaudeProvider(OPUS, client=Wire(lambda r: _error(529, "overloaded_error")).client()),
        ClaudeProvider(HAIKU, client=Wire(lambda r: _sse(_stream(["да"], "end_turn"))).client()),
    )
    assert [c async for c in chain.stream_text(system="s", messages=TALK)] == ["да"]


# =========================================================================== ключ и журналы


def _exc_text(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc)) + repr(exc) + repr(exc.__cause__)


@pytest.mark.parametrize(
    "reply",
    [
        lambda r: _error(401, "authentication_error"),
        lambda r: _error(400, "invalid_request_error"),
        lambda r: _error(500),
        _raise(httpx2.ConnectError("нет сети")),
        lambda r: httpx2.Response(200, json=_message([], stop_reason="refusal")),
        lambda r: _json_reply({"ok": True}),
    ],
    ids=["401", "400", "500", "сеть", "refusal", "успех"],
)
async def test_key_not_in_logs_or_exceptions(
    reply: Reply, caplog: pytest.LogCaptureFixture
) -> None:
    """CLAUDE.md: ключ — только в окружении; не попадает в журналы (даже DEBUG) и в тексты
    исключений с цепочкой причин."""
    caplog.set_level(logging.DEBUG)
    provider = ClaudeProvider(OPUS, client=Wire(reply).client())
    try:
        await _ask(provider)
    except LLMError as exc:
        assert FAKE_KEY not in _exc_text(exc)
        assert FAKE_KEY not in exc.user_message
    stream = ClaudeProvider(OPUS, client=Wire(reply).client())
    try:
        async for _ in stream.stream_text(system="s", messages=TALK):
            pass
    except Exception as exc:  # проверяем текст любой ошибки
        assert FAKE_KEY not in _exc_text(exc)
    assert caplog.records  # SDK действительно писал в журнал
    assert FAKE_KEY not in caplog.text


async def test_message_text_not_in_debug_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    await _ask(ClaudeProvider(OPUS, client=Wire(lambda r: _json_reply({"ok": True})).client()))
    assert TALK[0].content not in caplog.text


@pytest.mark.parametrize("other", ["ANTHROPIC_AUTH_TOKEN", None])
def test_key_only_from_anthropic_api_key(
    monkeypatch: pytest.MonkeyPatch, other: str | None
) -> None:
    """Без ANTHROPIC_API_KEY — LLMConfigError до сети, даже если задан другой способ входа SDK."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    if other:
        monkeypatch.setenv(other, FAKE_KEY)
    with pytest.raises(LLMConfigError) as info:
        ClaudeProvider(OPUS)
    assert "ANTHROPIC_API_KEY" in str(info.value)
    assert FAKE_KEY not in _exc_text(info.value)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with pytest.raises(LLMConfigError):
        ClaudeProvider(OPUS)
    with pytest.raises(LLMConfigError):
        claude_adapter.Opus()


# =========================================================================== стоимость


@pytest.mark.parametrize(
    ("model", "usage", "expected"),
    [
        (OPUS, Usage(1_000_000, 1_000_000), 24.0),
        (SONNET, Usage(2_000, 500), 0.009),
        (HAIKU, Usage(1_000_000, 0), 0.10),
        (HAIKU, Usage(0, 1_000_000), 0.50),
        # 1000 + 1000×1,25 (запись кэша) + 10000×0,1 (чтение) = 3250 вх. × 0,1 + 100 × 0,5
        (HAIKU, Usage(1_000, 100, 10_000, 1_000), 0.000375),
        (OPUS, Usage(0, 0, 1_000_000, 0), 0.4),
        (OPUS, Usage(0, 0, 0, 1_000_000), 5.0),
    ],
)
def test_cost_by_price(model: str, usage: Usage, expected: float) -> None:
    """ТЗ §10 «контроль расходов»: прайс $/1 млн токенов (Opus 4/20, Sonnet 2/10, Haiku 0,1/0,5)."""
    assert cost_usd(model, usage) == pytest.approx(expected, abs=1e-9)


async def test_cost_flows_into_result_and_adapter_metrics() -> None:
    usage = {
        "input_tokens": 3_000,
        "output_tokens": 1_000,
        "cache_read_input_tokens": 2_000,
        "cache_creation_input_tokens": 0,
    }
    wire = Wire(lambda r: _json_reply({"ok": True}, usage=usage))
    result = await _ask(ClaudeProvider(OPUS, client=wire.client()))
    # (3000 + 2000×0,1) × 4 + 1000 × 20 = 12800 + 20000 → 0,0328 $
    assert result.cost_usd == pytest.approx(0.0328)
    assert result.usage == Usage(3_000, 1_000, 2_000, 0)


def test_unknown_price_is_not_free() -> None:
    assert cost_usd("claude-opus-5-5-20261101", Usage(1_000_000, 1_000_000)) != 0.0


# =========================================================================== адаптер: «эхо» по HTTP


OWNER_OK = {
    "level": 2,
    "strategy": "find_need_prepare_message",
    "reply": "Слышу. Что задело сильнее всего?",
    "draft": "Мне грустно. Давай договоримся заранее.",
    "crisis_type": None,
    "crisis_actions": [],
}


def _echo_adapter() -> tuple[Any, Wire]:
    """Модель-«эхо» на уровне HTTP: агент получателя возвращает в adapted и reply_hint всё тело
    своего запроса; агент владельца — в reply."""
    holder: dict[str, Any] = {}

    def reply(request: httpx2.Request) -> httpx2.Response:
        raw = request.content.decode()
        if json.loads(raw)["system"] == holder["recipient_system"]:
            data = {"safety": "none", "tone": 3, "adapted": raw, "reply_hint": raw}
        else:
            data = {**OWNER_OK, "reply": raw}
        return _json_reply(data, usage={"input_tokens": 100, "output_tokens": 50})

    wire = Wire(reply)
    model = claude_adapter.Opus(provider=ClaudeProvider(OPUS, client=wire.client()))
    holder["recipient_system"] = model.recipient_system
    return model, wire


def _owner_strings(s: dict[str, Any]) -> list[str]:
    owner = s["owner"]
    return [
        *s["owner_turns"],
        *s.get("owner_memory", []),
        s.get("relationship", ""),
        owner["name"],
        owner["partner_name"],
    ]


@pytest.fixture(scope="module")
def situations() -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = run.load_situations()
    return loaded


def test_echo_delivery_body_has_only_original(situations: list[dict[str, Any]]) -> None:
    """ТЗ §6.2 последний абзац, §9.2 п.1: тело HTTP-запроса агента получателя — системный
    промпт и ровно один JSON {"original": …}; ни реплик, ни памяти, ни имён, ни отношений
    автора. Эхо этого тела в adapted не содержит данных автора."""
    model, wire = _echo_adapter()
    try:
        results = {r["id"]: r for r in run.run_suite(situations, model)}
    finally:
        model.close()
    recipient = [b for b in wire.bodies if b["system"] == model.recipient_system]
    with_pm = [s for s in situations if s.get("partner_message")]
    assert len(recipient) == len(with_pm)
    by_original = {s["partner_message"]["original"]: s for s in with_pm}
    for body in recipient:
        (msg,) = body["messages"]
        assert msg["role"] == "user"
        payload = json.loads(msg["content"])
        assert set(payload) == {"original"}
        s = by_original.pop(payload["original"])
        dumped = json.dumps(body["messages"], ensure_ascii=False)
        for item in _owner_strings(s):
            if item and item not in s["partner_message"]["original"]:
                assert item not in dumped, (s["id"], item[:40])
        adapted = results[s["id"]]["response"]["adapted"] or ""
        for item in _owner_strings(s):
            if item and item not in s["partner_message"]["original"]:
                assert item not in adapted, (s["id"], item[:40])
    assert by_original == {}


def test_echo_requests_have_no_ids_or_foreign_context(situations: list[dict[str, Any]]) -> None:
    """ТЗ §9.1 п.4, §9.2 п.1–2: ни в одном запросе нет служебных id ситуаций, заголовков,
    ожиданий, чужого личного контекста, metadata/user_id; только заголовки SDK."""
    model, wire = _echo_adapter()
    try:
        run.run_suite(situations, model)
    finally:
        model.close()
    assert wire.bodies
    everything = "\n".join(json.dumps(b, ensure_ascii=False) for b in wire.bodies)
    for s in situations:
        assert s["id"] not in everything
        assert s["title"] not in everything
        for secret in s.get("partner_private_context", []):
            assert secret not in everything, s["id"]
        for word in s["expected"].get("agent_should", []):
            assert word not in everything
    for body, headers in zip(wire.bodies, wire.headers, strict=True):
        assert set(body) <= ALLOWED_BODY_KEYS
        assert body["thinking"] == {"type": "adaptive"}
        assert "temperature" not in body and "top_p" not in body
        _check_headers(headers)
    for word in ("telegram", "user_id", "chat_id", "metadata"):
        assert word not in everything.lower()


@pytest.mark.parametrize(
    "reply",
    [
        lambda r: httpx2.Response(200, json=_message([], stop_reason="refusal")),
        lambda r: httpx2.Response(
            200, json=_message([{"type": "text", "text": "{"}], stop_reason="max_tokens")
        ),
        lambda r: httpx2.Response(200, json=_message([], stop_reason="end_turn")),
        lambda r: _json_reply({"level": 2}),  # JSON не по схеме — нет обязательных полей
        lambda r: _error(529, "overloaded_error"),
    ],
    ids=["refusal", "max_tokens", "пусто", "не по схеме", "перегрузка"],
)
def test_adapter_failure_is_failed_situation_not_pass(
    situations: list[dict[str, Any]], reply: Reply
) -> None:
    """Ошибка модели в адаптере → ситуация «не прошла» с причиной (§10), не «пройдено»."""
    model = claude_adapter.Opus(provider=ClaudeProvider(OPUS, client=Wire(reply).client()))
    sample = [s for s in situations if s["id"] in {"l1-01-dishes", "l5-03-outgoing-threat"}]
    try:
        rows = run.run_suite(sample, model)
    finally:
        model.close()
    assert len(rows) == 2
    for row in rows:
        assert not row["auto_pass"], row["id"]
        assert "model_error" in row["checks"]
        assert row["checks"]["model_error"]["detail"]  # причина видна в отчёте
        if row["metrics"]["calls"][0]["error"] is not None:  # ошибка на уровне API
            assert row["metrics"]["errors"]
            assert row["metrics"]["cost_usd"] == 0


def test_adapter_metrics_sum_owner_and_recipient(situations: list[dict[str, Any]]) -> None:
    """§10 «контроль расходов»: стоимость ситуации — сумма вызовов агента владельца и
    получателя по прайсу (Opus 4/20: 100 вх. + 50 вых. = 0,0014 $ на вызов)."""
    model, _ = _echo_adapter()
    sample = [s for s in situations if s["id"] == "l1-01-dishes"]
    assert sample[0].get("partner_message")
    try:
        (row,) = run.run_suite(sample, model)
    finally:
        model.close()
    m = row["metrics"]
    assert [c["role"] for c in m["calls"]] == ["owner", "recipient"]
    assert m["input_tokens"] == 200 and m["output_tokens"] == 100
    assert m["cost_usd"] == pytest.approx(2 * 0.0014)


# =========================================================================== CLI без ключа


def _env(**extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("ANTHROPIC_") and k not in {"PYTHONPATH"}
    }
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    env.update(extra)
    return env


def _cli(script: str, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — фиксированная команда, аргументы из теста
        [sys.executable, str(EVALS_DIR / script), *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _reports_snapshot() -> dict[str, int]:
    return {str(p): p.stat().st_mtime_ns for p in (EVALS_DIR / "reports").rglob("*")}


CLI_CASES = [
    ("run.py", ("--model", "adapters.claude:Opus", "--label", "a8")),
    ("compare.py", ("--label", "a8")),
    ("compare.py", ("--models", "adapters.claude:Haiku", "--label", "a8")),
]


@pytest.mark.parametrize("key", [None, ""], ids=["нет ключа", "пустой ключ"])
@pytest.mark.parametrize(("script", "args"), CLI_CASES, ids=["run", "compare", "compare-haiku"])
def test_cli_without_key_fails_and_writes_nothing(
    tmp_path: Path, script: str, args: tuple[str, ...], key: str | None
) -> None:
    out = tmp_path / "out"
    before = _reports_snapshot()
    env = _env() if key is None else _env(ANTHROPIC_API_KEY=key)
    proc = _cli(script, *args, "--out-dir", str(out), env=env)
    assert proc.returncode != 0
    assert proc.returncode == 3, proc.stderr
    assert "ANTHROPIC_API_KEY" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert not out.exists() or list(out.iterdir()) == []
    assert _reports_snapshot() == before


class _FakeApi(BaseHTTPRequestHandler):
    status = 401
    seen: ClassVar[list[str]] = []

    def do_POST(self) -> None:  # имя метода задаёт http.server
        self.rfile.read(int(self.headers.get("content-length", 0)))
        self.seen.append(self.path)
        kind = "authentication_error" if self.status == 401 else "overloaded_error"
        body = json.dumps({"type": "error", "error": {"type": kind, "message": "x"}}).encode()
        self.send_response(self.status)
        self.send_header("content-type", "application/json")
        self.send_header("x-should-retry", "false")  # без повторов SDK — быстро
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def fake_api(request: pytest.FixtureRequest) -> Iterator[tuple[str, type[_FakeApi]]]:
    handler = type("Api", (_FakeApi,), {"status": request.param, "seen": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", handler
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("script", "args", "fake_api"),
    [
        pytest.param(*CLI_CASES[0], 401, id="run-ключ отклонён"),
        pytest.param(*CLI_CASES[0], 529, id="run-перегрузка"),
        pytest.param(*CLI_CASES[1], 401, id="compare-ключ отклонён"),
        pytest.param(*CLI_CASES[1], 529, id="compare-перегрузка"),
    ],
    indirect=["fake_api"],
)
def test_cli_with_rejected_key_or_outage_fails_without_report(
    tmp_path: Path, script: str, args: tuple[str, ...], fake_api: tuple[str, type[_FakeApi]]
) -> None:
    """Ключ отклонён (401) — прогон останавливается; модель недоступна на всех ситуациях —
    «не ответила ни на одну». Код 3, отчёт не пишется, ключ не печатается (§10, CLAUDE.md)."""
    url, handler = fake_api
    out = tmp_path / "out"
    env = _env(ANTHROPIC_API_KEY=FAKE_KEY, ANTHROPIC_BASE_URL=url)
    proc = _cli(script, *args, "--out-dir", str(out), env=env)
    assert handler.seen, "запрос не дошёл до локального сервера"
    assert proc.returncode == 3, (proc.returncode, proc.stderr[-500:])
    assert FAKE_KEY not in proc.stdout + proc.stderr
    assert not out.exists() or list(out.iterdir()) == []


def test_echo_owner_steps_do_not_see_approved_original(situations: list[dict[str, Any]]) -> None:
    model, wire = _echo_adapter()
    key = [s for s in situations if s.get("dialog")]
    assert key
    try:
        run.run_suite(key, model)
    finally:
        model.close()
    recipient = model.recipient_system
    owner = [b for b in wire.bodies if b["system"] != recipient]
    steps = owner[1:]  # первый вызов агента владельца — respond, дальше — шаги converse
    assert len(steps) == len(key[0]["owner_turns"])
    original = key[0]["partner_message"]["original"]
    for body in steps:
        dumped = json.dumps(body["messages"], ensure_ascii=False)
        assert original not in dumped

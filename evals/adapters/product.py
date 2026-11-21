"""Адаптер продуктового агента к эталонному набору (задача 0.5): ``adapters.product:V0``.

В отличие от ``adapters.claude`` (промпт сравнения моделей ``compare.v1``, один вызов агента
получателя) здесь работает **код продукта** ``backend/app/ai/`` с промптами v0:

- ``respond`` — ``dialog.reply`` по репликам владельца (вторая линия безопасности, черновик
  отдельным вызовом, кризисный шаблон). Если в ситуации есть одобренное **исходящее**
  сообщение, оно проходит конвейер (как после «Отправить»); остановленное безопасностью
  сообщение даёт автору ответ по протоколу ``outgoing_threat`` и т. п. — это часть ответа агента
  владельца в продукте.
- ``converse`` — ``dialog.reply`` после каждой реплики; история — прошлые ответы агента.
- ``deliver`` — ``pipeline.process`` **только по оригиналу** (безопасность → тон → адаптация →
  контроль смысла; доставку решает код конвейера).

Модели — гипотеза АР-19 через OpenRouter: Sonnet 5.5 — диалог, черновик, адаптация; Haiku 5.5 —
классификаторы безопасности, тона, смысла (без резервной модели: стоимость и качество
относятся к названным моделям). Потолок расходов ``compare.py --max-cost-usd`` проверяется
перед каждым вызовом (``Metered``). Ошибки — как у ``adapters.claude``: фатальные (ключ, лимит,
некорректный запрос) — ``RunAborted``; остальные — ``ModelCallFailed`` (ситуация «не прошла»)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from typing import Any, TypeVar

from app.ai import crisis, dialog, pipeline
from app.ai.dialog import OwnerContext, PairMessage, Turn
from app.ai.llm import (
    ChatMessage,
    LLMError,
    LLMProvider,
    OpenRouterProvider,
    Price,
    StructuredResult,
    Usage,
    fetch_price,
)
from app.ai.models import OPENROUTER_HAIKU, OPENROUTER_SONNET, AgentModels
from app.ai.prompts import V0 as PROMPTS_V0
from app.ai.prompts import V1 as PROMPTS_V1
from app.ai.prompts import PromptSet

from adapters.claude import (
    FATAL_KINDS,
    SCHEMA_RESERVE_CHARS,
    CostBudget,
    ModelCallFailed,
    RunAborted,
    _run_module,
)

T = TypeVar("T")

REPORT_NOTE = (
    "Продуктовый конвейер `backend/app/ai/` (задача 0.5): безопасность — отдельный "
    "классификатор (Haiku), тон — Haiku, адаптация — Sonnet, контроль смысла — Haiku; агент "
    "владельца — Sonnet, вторая линия безопасности по репликам — Haiku. Время — сумма вызовов "
    "(диалог и вторая линия безопасности идут параллельно, поэтому сумма больше реального)."
)


class Metered:
    """Обёртка поставщика: потолок расходов до вызова и метрики (без текстов) после."""

    def __init__(self, inner: LLMProvider, owner: ProductAdapter) -> None:
        self._inner = inner
        self._owner = owner

    @property
    def model(self) -> str:
        return self._inner.model

    def _ceiling(self, system: str, messages: Sequence[ChatMessage], max_tokens: int) -> float:
        price = getattr(self._inner, "price", None)
        if not isinstance(price, Price):
            return 0.0
        chars = len(system) + sum(len(m.content) for m in messages) + SCHEMA_RESERVE_CHARS
        return (chars * price.input_per_mtok + max_tokens * price.output_per_mtok) / 1e6

    async def complete_json(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        schema: Mapping[str, Any],
        max_tokens: int = 16000,
    ) -> StructuredResult:
        budget = self._owner.budget
        if budget is not None:
            budget.check(self.model, self._ceiling(system, messages, max_tokens) or None)
        try:
            result = await self._inner.complete_json(
                system=system, messages=messages, schema=schema, max_tokens=max_tokens
            )
        except LLMError as exc:
            self._owner.record(self.model, None, exc)
            raise
        self._owner.record(self.model, result, None)
        return result

    def stream_text(
        self, *, system: str, messages: Sequence[ChatMessage], max_tokens: int = 16000
    ) -> AsyncIterator[str]:
        return self._inner.stream_text(system=system, messages=messages, max_tokens=max_tokens)


def _context(request: Any) -> OwnerContext:
    """Контекст агента владельца — только поля EvalRequest (данные владельца и сообщение пары)."""
    pm = request.partner_message
    return OwnerContext(
        owner_name=request.owner_name,
        partner_name=request.partner_name,
        relationship=request.relationship,
        memory=tuple(request.owner_memory),
        pair_messages=(PairMessage(pm.direction, pm.original),) if pm is not None else (),
    )


class ProductAdapter:
    name = "product.v0"
    prompt_set: PromptSet = PROMPTS_V0
    report_note = REPORT_NOTE

    def __init__(
        self,
        writer: LLMProvider | None = None,
        classifier: LLMProvider | None = None,
    ) -> None:
        if writer is None or classifier is None:
            OpenRouterProvider(OPENROUTER_SONNET)  # без ключа — LLMConfigError до сети

            async def prices() -> tuple[Price, Price]:
                return await asyncio.gather(
                    fetch_price(OPENROUTER_SONNET), fetch_price(OPENROUTER_HAIKU)
                )

            w_price, c_price = asyncio.run(prices())
            writer = writer or OpenRouterProvider(OPENROUTER_SONNET, price=w_price)
            classifier = classifier or OpenRouterProvider(OPENROUTER_HAIKU, price=c_price)
        self.models = AgentModels.split(Metered(writer, self), Metered(classifier, self))
        self.budget: CostBudget | None = None  # задаёт compare.py (--max-cost-usd)
        self._runner = asyncio.Runner()
        self._calls: list[dict[str, Any]] = []
        self._deliveries: dict[str, pipeline.Delivery] = {}

    # ------------------------------------------------------------------ учёт

    def record(self, model: str, result: StructuredResult | None, exc: LLMError | None) -> None:
        usage: Usage | None = None
        cost = 0.0
        if result is not None:
            cost, usage = result.cost_usd, result.usage
        elif exc is not None:
            cost, usage = exc.cost_usd or 0.0, exc.usage
        if self.budget is not None:
            self.budget.add(model, cost)
        tokens_in = (
            usage.input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens
            if usage
            else 0
        )
        self._calls.append(
            {
                "model": result.model if result else model,
                "latency_s": result.latency_s if result else 0.0,
                "input_tokens": tokens_in,
                "output_tokens": usage.output_tokens if usage else 0,
                "cost_usd": cost,
                "fallback_used": result.fallback_used if result else False,
                "error": exc.kind if exc else None,
            }
        )

    def _run(self, coro: Awaitable[T]) -> T:
        try:
            return self._runner.run(coro)  # type: ignore[arg-type]
        except LLMError as exc:
            if exc.kind in FATAL_KINDS:
                raise RunAborted(f"{exc.kind}: {exc.detail}", kind=exc.kind) from exc
            raise ModelCallFailed(exc.kind) from exc

    # ------------------------------------------------------------------ интерфейс run.py

    def _owner_response(
        self, request: Any, result: dialog.DialogResult, notice: crisis.CrisisResponse | None
    ) -> Any:
        resp_cls = _run_module(request).EvalResponse
        response = result.crisis
        reply = result.reply
        if notice is not None:  # одобренное исходящее остановлено — ответ автору по протоколу
            types = (*(response.types if response else ()), *notice.types)
            primary = response.primary if response else notice.primary
            response = crisis.respond(types, primary=primary, prompt_set=self.prompt_set)
            reply = f"{reply}\n\n{notice.text}" if notice.text not in reply else reply
        if response is None:
            return resp_cls(
                level=result.level,
                strategy=result.strategy,
                reply=reply,
                draft=result.draft.text if result.draft else None,
            )
        return resp_cls(
            level=5,
            strategy=dialog.STRATEGIES[5],
            reply=reply,
            draft=None,
            crisis_type=response.primary,
            crisis_actions=response.actions,
            safety_event=True,
            partner_notified=False,
        )

    def _process(self, original: str) -> pipeline.Delivery:
        if original not in self._deliveries:
            approved = pipeline.approve(original, confirmed=True)  # одобрено автором в ситуации
            self._deliveries[original] = self._run(
                pipeline.process(self.models, approved, prompt_set=self.prompt_set)
            )
        return self._deliveries[original]

    def respond(self, request: Any) -> Any:
        context = _context(request)
        turns = [Turn("owner", t) for t in request.owner_turns]
        result = self._run(dialog.reply(self.models, context, turns, prompt_set=self.prompt_set))
        notice = None
        pm = request.partner_message
        if pm is not None and pm.direction == "outgoing":
            notice = self._process(pm.original).author_notice
        return self._owner_response(request, result, notice)

    def converse(self, request: Any) -> list[Any]:
        context = _context(request)
        history: list[Turn] = []
        responses = []
        for text in request.owner_turns:
            history.append(Turn("owner", text))
            result = self._run(
                dialog.reply(self.models, context, list(history), prompt_set=self.prompt_set)
            )
            history.append(Turn("agent", result.reply))
            responses.append(self._owner_response(request, result, None))
        return responses

    def deliver(self, request: Any) -> Any:
        delivery = self._process(request.original)
        return _run_module(request).DeliveryResponse(
            delivery=delivery.status,
            tone=delivery.tone,
            adapted=delivery.shown if delivery.is_adapted else None,
            safety_flag=delivery.status == "withheld",
            reply_hint=delivery.reply_hint if delivery.delivered else None,
        )

    def pop_metrics(self) -> dict[str, Any]:
        calls, self._calls = self._calls, []
        self._deliveries.clear()
        ok = [c for c in calls if c["error"] is None]
        return {
            "prompt": f"product.{self.prompt_set.label()}",
            "calls": calls,
            "latency_s": round(sum(c["latency_s"] for c in ok), 3),
            "input_tokens": sum(c["input_tokens"] for c in calls),
            "output_tokens": sum(c["output_tokens"] for c in calls),
            "cost_usd": round(sum(c["cost_usd"] for c in calls), 8),
            "errors": [c["error"] for c in calls if c["error"] is not None],
        }

    def close(self) -> None:
        self._runner.close()


class V0(ProductAdapter):
    name = "product.v0"
    prompt_set = PROMPTS_V0


class V1(ProductAdapter):
    """v0 + safety.v1 и meaning.v1 (исправления по прогону v0)."""

    name = "product.v1"
    prompt_set = PROMPTS_V1

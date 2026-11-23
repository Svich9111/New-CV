"""Консольный прототип: сценарий «Анна и Игорь» (ТЗ §1; задача 0.5) — без интерфейса и БД.

Запуск из ``backend/``::

    uv run python -m app.ai.demo --offline          # заглушка модели, без сети
    OPENROUTER_API_KEY=… uv run python -m app.ai.demo   # Sonnet + Haiku через OpenRouter

Шаги: диалог Анны с её агентом → черновик → правка Анной → явное подтверждение «Отправить» →
конвейер §6.2 (безопасность → тон → адаптация → контроль смысла) → что видит Игорь (пометка
«адаптировано», оригинал по запросу, подсказка, как ответить).

Без подтверждения ничего не доставляется: в терминале спрашивается «Отправить?»; без терминала
(CI, тесты) подтверждение — только флаг ``--send``, иначе сообщение не уходит. Все люди и
события выдуманы; в выводе нет идентификаторов (только имена из сценария), журналов с текстами
модуль не пишет."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.ai import dialog, pipeline
from app.ai.dialog import OwnerContext, Turn
from app.ai.llm import LLMError
from app.ai.models import AgentModels, openrouter_models
from app.ai.stub import ScriptedProvider, demo_handler

OWNER = "Анна"
OWNER_GEN = "Анны"
PARTNER = "Игорь"
PARTNER_GEN = "Игоря"
CONTEXT = OwnerContext(
    owner_name=OWNER,
    partner_name=PARTNER,
    relationship="Вместе три года, живут вместе",
    memory=(
        "Любит планировать совместные вечера заранее",
        "Болезненно реагирует, когда планы меняются в последний момент",
    ),
)
TURNS = (
    "Ему вообще плевать на меня.",
    "Игорь третий раз за месяц отменил наш вечер. Сегодня написал за час до кино, что "
    "задерживается на работе. Обиднее всего, что я узнаю в последний момент.",
)
# Правка Анны (ключевой сценарий ТЗ §1): переписывает вариант агента своими словами и
# добавляет свою резкую фразу — её агент получателя смягчит.
ANNA_VERSION = (
    "Мне грустно, когда наши планы отменяются. Мне важно проводить с тобой время. Но ты уже "
    "третий раз за месяц отменяешь вечер, сегодня — за час до кино! Пожалуйста, предупреждай "
    "меня заранее, если не успеваешь."
)


@dataclass(frozen=True)
class Outcome:
    sent: bool
    delivery: pipeline.Delivery | None
    cost_usd: float
    calls: int


def anna_edits(draft_text: str) -> str:
    return ANNA_VERSION


def _line(out: Callable[[str], None], who: str, text: str) -> None:
    out(f"{who}: {text}")


async def scenario(
    models: AgentModels,
    *,
    confirm: Callable[[str], bool],
    edit: Callable[[str], str] = anna_edits,
    out: Callable[[str], None] = print,
) -> Outcome:
    cost, calls = 0.0, 0
    history: list[Turn] = []
    result: dialog.DialogResult | None = None
    out(f"── Личный чат: {OWNER} и её агент (Игорь этот чат не видит)")
    for text in TURNS:
        history.append(Turn("owner", text))
        _line(out, OWNER, text)
        result = await dialog.reply(models, CONTEXT, history)
        cost, calls = cost + result.cost_usd, calls + len(result.calls)
        _line(out, f"Агент {OWNER_GEN}", result.reply)
        out(f"   [уровень {result.level} · {result.strategy}]")
        history.append(Turn("agent", result.reply))
        if result.crisis is not None:
            out("── Кризисный протокол: черновика нет, ничего не отправляется.")
            return Outcome(False, None, cost, calls)

    if result is None or result.draft is None:
        out("── Агент пока не предложил черновик — отправлять нечего.")
        return Outcome(False, None, cost, calls)
    out("── Черновик («чувство — ситуация — потребность — просьба»), не отправлен:")
    out(f"   {result.draft.text}")
    edited = edit(result.draft.text)
    out(f"── {OWNER} правит текст:")
    out(f"   {edited}")

    if not confirm(edited):
        out("── Не отправлено: без явного «Отправить» сообщение не уходит.")
        return Outcome(False, None, cost, calls)
    approved = pipeline.approve(edited, confirmed=True)
    out(f"── {OWNER} нажала «Отправить». Оригинал зафиксирован.")

    delivery = await pipeline.process(models, approved)
    cost, calls = cost + delivery.cost_usd, calls + len(delivery.calls)
    out(
        f"── Конвейер: безопасность — {delivery.safety_category}; тон — {delivery.tone}; "
        f"итог — {delivery.status}"
        + (
            f"; расхождение смысла: {', '.join(delivery.meaning_problems)}"
            if delivery.meaning_problems
            else ""
        )
    )
    if not delivery.delivered:
        out(f"── {PARTNER} ничего не получает и о срабатывании не узнаёт.")
        if delivery.author_notice is not None:
            _line(out, f"Агент {OWNER_GEN}", delivery.author_notice.text)
        return Outcome(False, delivery, cost, calls)

    out(f"── Экран {PARTNER_GEN}: сообщение от {OWNER_GEN}")
    mark = " [адаптировано]" if delivery.is_adapted else ""
    out(f"   {delivery.shown}{mark}")
    if delivery.harsh_tone_warning:
        out(f"   ⚠ {pipeline.WARNING_HARSH_TONE}")
    if delivery.reply_hint:
        _line(out, f"   Агент {PARTNER_GEN} (подсказка)", delivery.reply_hint)
    if delivery.is_adapted:
        out(f"── {PARTNER} нажимает «Показать оригинал»:")
        out(f"   {delivery.original}")
    return Outcome(True, delivery, cost, calls)


def _ask(text: str) -> bool:
    answer = input("Отправить? [да/нет]: ").strip().casefold()
    return answer in ("да", "д", "yes", "y")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Прототип «Анна и Игорь» (задача 0.5)")
    parser.add_argument("--offline", action="store_true", help="заглушка модели, без сети")
    parser.add_argument(
        "--send", action="store_true", help="подтвердить «Отправить» без вопроса (без терминала)"
    )
    args = parser.parse_args(argv)
    if args.send:
        confirm: Callable[[str], bool] = lambda _text: True  # noqa: E731
    elif sys.stdin.isatty():
        confirm = _ask
    else:
        confirm = lambda _text: False  # noqa: E731 — без терминала и без --send не отправляем
    try:
        models = (
            AgentModels.single(ScriptedProvider(demo_handler))
            if args.offline
            else openrouter_models()
        )
        outcome = asyncio.run(scenario(models, confirm=confirm))
    except LLMError as exc:
        print(f"Ошибка модели ({exc.kind}): {exc.user_message}", file=sys.stderr)
        return 3
    print(f"── Вызовов модели: {outcome.calls}, стоимость: ${outcome.cost_usd:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

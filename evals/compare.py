"""Предварительное сравнение моделей на эталонном наборе (задача 0.4, АР-19).

Запуск: ``cd backend && uv run python ../evals/compare.py`` (по умолчанию Opus, Sonnet и Haiku
через ``evals/adapters/claude.py``) или ``--models adapters.claude:Opus adapters.claude:Haiku``.

Прогоняет модели подряд тем же кодом, что ``run.py``, пишет отчёт каждой модели и сводную
таблицу ``<дата>_compare.md/.json`` в ``evals/reports/``: доля автопроверок по уровням,
автопризнаки кризисного протокола, p50/p95 времени, $ на ситуацию и на 30 ситуаций.

Это предварительное сравнение по черновику набора (АР-19): окончательный выбор основной и
резервной модели — после утверждения набора психологом (Д5, АР-15). «Кризис распознан»
(ТЗ §12 п.6) считается только по оценке эксперта, а не по автопризнакам.

Все модели загружаются до первого вызова: без ``ANTHROPIC_API_KEY`` — понятная ошибка, код
выхода 3, ни одного отчёта и никаких выдуманных результатов.

Модели разных поставщиков — через OpenRouter (задача 0.4b, АР-22):
``--models adapters.openrouter:anthropic/claude-haiku-5.5``
``adapters.openrouter:openai/gpt-5.6-terra``
(имя после двоеточия — идентификатор модели OpenRouter).

Потолок расходов (``--max-cost-usd``, по умолчанию $15) — общий на весь прогон: перед каждым
вызовом модели проверяется, что потраченное плюс резерв на вызов не больше потолка. Если нет —
прогон останавливается до превышения (код выхода 5), отчёты уже прогнанных моделей и сводка
по ним сохраняются.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

EVALS_DIR = Path(__file__).resolve().parent
for _path in (EVALS_DIR, EVALS_DIR.parent / "backend"):  # adapters.* импортируют app.ai.llm
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import run  # noqa: E402 — путь к evals добавлен выше
from adapters.claude import BudgetExceeded, CostBudget  # noqa: E402

DEFAULT_MAX_COST_USD = 15.0
RESCORE_NOTE = "Пересчитано после Б-35 (ответы те же, модели не вызывались)."


def rescore(summary_path: Path, situations_dir: Path, note: str) -> int:
    """Пересчёт сводки и отчётов моделей по сохранённым ответам (без платных вызовов). Файлы
    перезаписываются на месте — исходные остаются в истории git. Код выхода: 0 — пересчитано;
    1 — нет файлов или набор изменился."""
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        situations = run.load_situations(situations_dir)
    except (OSError, ValueError, run.SituationError) as exc:
        print(f"Пересчёт невозможен: {exc}", file=sys.stderr)
        return 1
    out_dir = summary_path.parent
    reports = []
    for row in summary["rows"]:
        path = out_dir / f"{row['report']}.json"
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            report = run.rescore_report(report, situations, situations_dir, note=note)
        except (OSError, ValueError, run.SituationError) as exc:
            print(f"Пересчёт {path.name} невозможен: {exc}", file=sys.stderr)
            return 1
        reports.append(report)
    for report in reports:
        run.write_report(report, out_dir)
    rows = [summary_row(r) for r in reports]
    blind = summary.get("blind_key")
    aliases = {name: alias for alias, name in blind.items()} if blind else {}
    table_rows = [{**r, "model": aliases.get(r["model"], r["model"])} for r in rows]
    meta = {k: v for k, v in summary.items() if k not in ("notice", "rows", "blind_key")}
    meta["rescored"] = note
    new_summary = {**summary, "rows": rows, "rescored": note}
    summary_path.write_text(
        json.dumps(new_summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path = summary_path.with_suffix(".md")
    md_path.write_text(render_table(table_rows, meta), encoding="utf-8")
    print(f"Пересчитано: {md_path}")
    return 0


DEFAULT_MODELS = ("adapters.claude:Opus", "adapters.claude:Sonnet", "adapters.claude:Haiku")


def summary_row(report: dict[str, Any]) -> dict[str, Any]:
    s = report["summary"]
    m = report.get("metrics") or {}
    return {
        "model": report["model"],
        "report": report["label"],
        "auto_pass_rate": s["auto_pass_rate"],
        "by_level": {lvl: row["rate"] for lvl, row in s["by_level"].items()},
        "crisis_auto": f"{s['crisis']['recognized']}/{s['crisis']['total']}",
        "false_crisis": len(s["false_crisis"]),
        "latency_p50_s": m.get("latency_p50_s"),
        "latency_p95_s": m.get("latency_p95_s"),
        "cost_usd_per_situation": m.get("cost_usd_per_situation"),
        "cost_usd_per_30": m.get("cost_usd_per_30"),
        "cost_usd_total": m.get("cost_usd_total"),
        "with_errors": m.get("with_errors"),
        "not_verified": len(s["not_verified"]),
        "comparison": _comparison_cell(report.get("comparison")),
        "error_kinds": dict(
            sorted(
                Counter(
                    kind
                    for r in report["results"]
                    for kind in (r.get("metrics") or {}).get("errors", [])
                ).items()
            )
        ),
    }


def _comparison_cell(cmp_: dict[str, Any] | None) -> dict[str, Any] | None:
    """Итог сравнения с ``--baseline`` для сводки (без деталей по ситуациям)."""
    if cmp_ is None:
        return None
    keys = ("baseline", "baseline_model", "verdict", "auto_pass_rate_delta", "crisis_rate_delta")
    return {
        **{k: cmp_.get(k) for k in keys},
        "regressions": list(cmp_.get("regressions", [])),
        "improvements": list(cmp_.get("improvements", [])),
    }


def _num(value: float | None, fmt: str) -> str:
    return "—" if value is None else format(value, fmt)


def _comparison_lines(rows: Sequence[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for r in rows:
        c = r.get("comparison")
        if not c:
            continue
        deltas = (
            f"доля прохождения {c['auto_pass_rate_delta']:+}, кризисы {c['crisis_rate_delta']:+}"
            if c["auto_pass_rate_delta"] is not None and c["crisis_rate_delta"] is not None
            else "доли: нет данных"
        )
        lines += [
            f"Сравнение `{r['model']}` с `{c['baseline']}` (модель `{c['baseline_model']}`): "
            f"**{c['verdict']}** — {deltas}; регрессии: {', '.join(c['regressions']) or 'нет'}; "
            f"улучшения: {', '.join(c['improvements']) or 'нет'}.",
            "",
        ]
    return lines


def render_table(rows: Sequence[dict[str, Any]], meta: dict[str, Any]) -> str:
    lines = [
        (
            f"# Прогон промптов продукта — {meta['label']}"
            if meta.get("report_note")
            else f"# Предварительное сравнение моделей — {meta['label']}"
        ),
        "",
        f"Дата: {meta['generated_on']} · набор: `{meta['dataset_hash']}` "
        f"({meta['dataset_status']}) · ситуаций: {meta['situations']} · промпт: "
        f"`{meta['prompt']}`",
        "",
        "> **Предварительно (АР-19).** Набор — черновик A3 до утверждения психологом (Д5, АР-15); "
        "окончательный выбор основной и резервной модели — после утверждения. Кризисы — "
        "автопризнаки протокола; «кризис распознан» (ТЗ §12 п.6) — только по оценке эксперта.",
        "",
        *(
            [
                "> **Сравнение неполное:** не ответили — "
                + (", ".join(f"`{n}`" for n in meta["not_answered"]) or "—")
                + ("; прогон прерван фатальной ошибкой." if meta["incomplete"] else "."),
                "",
            ]
            if meta.get("not_answered") or meta.get("incomplete")
            else []
        ),
        *(
            [
                f"> **Прогон остановлен потолком расходов** ${meta['max_cost_usd']:.2f} "
                "(до превышения): не прогнаны — "
                + (", ".join(f"`{n}`" for n in meta.get("not_run", [])) or "—")
                + ".",
                "",
            ]
            if meta.get("budget_stopped")
            else []
        ),
        *(
            [
                "> **Модель пропущена (ошибка только этой модели):** "
                + "; ".join(f"`{n}` — {why}" for n, why in meta["skipped"].items()),
                "",
            ]
            if meta.get("skipped")
            else []
        ),
        "| Модель | Все автопроверки | Ур. 1 | Ур. 2 | Ур. 3 | Ур. 4 | Ур. 5 | Кризисы (авто) "
        "| Ложные кризисы | Не проверено | p50 / p95, с | $ на ситуацию | $ на 30 ситуаций "
        "| $ всего | Ошибки модели |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- "
        "| --- |",
    ]
    for r in rows:
        levels = " | ".join(run._pct(r["by_level"][str(lvl)]) for lvl in range(1, 6))
        errors = "—" if r["with_errors"] is None else str(r["with_errors"])
        kinds = r.get("error_kinds") or {}
        if kinds:
            errors += " (" + ", ".join(f"{k}: {n}" for k, n in kinds.items()) + ")"
        lines.append(
            f"| `{r['model']}` | {run._pct(r['auto_pass_rate'])} | {levels} | {r['crisis_auto']} "
            f"| {r['false_crisis']} | {r.get('not_verified', 0)} | "
            f"{_num(r['latency_p50_s'], '.1f')} / "
            f"{_num(r['latency_p95_s'], '.1f')} | {_num(r['cost_usd_per_situation'], '.4f')} | "
            f"{_num(r['cost_usd_per_30'], '.2f')} | {_num(r.get('cost_usd_total'), '.4f')} | "
            f"{errors} |"
        )
    total = sum(float(r.get("cost_usd_total") or 0) for r in rows)
    lines += ["", f"Расходы прогона по моделям в таблице: ${total:.4f}."]
    if meta.get("spent_usd") is not None:  # включая прерванную потолком модель (MINOR-8)
        lines.append(
            f"Потрачено всего (включая прерванную модель): ${meta['spent_usd']:.4f} "
            f"из ${meta['max_cost_usd']:.2f}."
        )
    if meta.get("rescored"):
        lines += ["", f"> **{meta['rescored']}**"]
    lines += [
        "",
        "Время — сумма вызовов на ситуацию (агент владельца + отдельный вызов агента получателя "
        "только по оригиналу). Стоимость — `usage.cost` ответа OpenRouter (или прайс "
        "`backend/app/ai/llm/pricing.py` для прямого Claude API), включая оплаченные неудачные "
        "вызовы (отказ, обрезанный ответ). «Не проверено» — ситуации, где автопроверки не "
        "смогли дать итог. Ошибки модели — число ситуаций с ошибкой вызова (в скобках — виды "
        "ошибок по вызовам: refusal — отказ, bad_output — обрезанный/невалидный ответ, "
        "unavailable — недоступность).",
        "",
        "p50/p95 — полное время двух вызовов на ситуацию без потока (агент владельца и агент "
        "получателя), а не время до первого символа: с требованием ТЗ §10 «до первого символа "
        "≤5 с в 95%» эти цифры не сопоставимы (ревью 0.4b MINOR-7).",
        "",
        meta.get("report_note")
        or (
            "Ограничение (ревью 0.4 MINOR-8): безопасность и адаптацию оценивает один вызов "
            "агента получателя, а не отдельный классификатор §6.2 п.3 (задача 3.2) — цифры "
            "«доставка» и «Кризисы (авто)» нельзя переносить на продуктовый конвейер."
        ),
        "",
        *_comparison_lines(rows),
        "Отчёты моделей (детали по ситуациям и таблица для психолога):",
        "",
        *[f"- `{r['report']}.md`" for r in rows],
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Коды выхода: 0 — все модели ответили; 1 — ошибка набора или параметров; 3 — модель не
    загрузилась, прогон прерван фатальной ошибкой или какая-то модель не ответила ни на одну
    ситуацию; 4 — какая-то модель ответила не на все ситуации; 5 — прогон остановлен потолком
    расходов (до превышения). Отчёты уже прогнанных моделей пишутся
    сразу и не теряются при ошибке следующей (ревью 0.4 MINOR-6, A8 D-3)."""
    parser = argparse.ArgumentParser(description="Сравнение моделей на эталонном наборе")
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument("--situations", type=Path, default=run.SITUATIONS_DIR)
    parser.add_argument("--out-dir", type=Path, default=run.REPORTS_DIR)
    parser.add_argument("--label", help="имя сводки; по умолчанию <дата>_compare")
    parser.add_argument(
        "--blind", action="store_true", help="отчёты моделей для эксперта без имени модели"
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=DEFAULT_MAX_COST_USD,
        help=f"потолок расходов на весь прогон, $ (по умолчанию {DEFAULT_MAX_COST_USD:g})",
    )
    parser.add_argument(
        "--rescore",
        type=Path,
        metavar="СВОДКА.json",
        help="пересчитать сохранённые ответы сводки текущими автопроверками, без вызова моделей",
    )
    parser.add_argument("--rescore-note", default=RESCORE_NOTE, help="пометка пересчёта")
    parser.add_argument(
        "--baseline",
        type=Path,
        metavar="ОТЧЁТ.json",
        help="отчёт модели (run.py/compare.py) для сравнения каждой модели прогона с ним",
    )
    args = parser.parse_args(argv)
    if args.rescore is not None:
        return rescore(args.rescore, args.situations, args.rescore_note)
    if args.max_cost_usd <= 0:
        print("--max-cost-usd должен быть больше нуля", file=sys.stderr)
        return 1
    baseline: dict[str, Any] | None = None
    if args.baseline is not None:
        try:
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"Отчёт для сравнения не прочитан: {exc}", file=sys.stderr)
            return 1

    try:
        situations = run.load_situations(args.situations)
    except run.SituationError as exc:
        print(f"Ошибка набора: {exc}", file=sys.stderr)
        return 1
    models = []
    for spec in args.models:  # все модели — до первого вызова: без ключа ничего не запускается
        try:
            models.append(run.load_model(spec))
        except Exception as exc:
            print(f"Модель {spec} не загружена: {exc}", file=sys.stderr)
            return 3

    budget = CostBudget(args.max_cost_usd)
    for model in models:
        if hasattr(model, "budget"):
            model.budget = budget
    label = run._safe_label(args.label or f"{date.today().isoformat()}_compare")
    # Слепой режим (ревью 0.4 MINOR-5, 0.3 R2-MINOR-4): модели — «model-A/B/C» в именах файлов и
    # в сводке md; соответствие — только в json сводки (blind_key).
    aliases = {m.name: f"model-{chr(ord('A') + i)}" for i, m in enumerate(models)}
    reports: list[dict[str, Any]] = []
    silent: list[str] = []  # модели, не ответившие ни на одну ситуацию
    skipped: dict[str, str] = {}  # модели с фатальной ошибкой только своей (400 и т. п.)
    not_run: list[str] = []
    budget_stopped = False
    code = 0
    for i, model in enumerate(models):
        tag = aliases[model.name] if args.blind else model.name
        model_label = run._safe_label(f"{label}_{tag}")
        try:
            report = run.build_report(situations, model, model_label, args.situations)
        except BudgetExceeded as exc:  # стоп до превышения: прогнанное сохраняется
            print(f"Прогон остановлен на {model.name}: {exc}", file=sys.stderr)
            budget_stopped = True
            not_run = [m.name for m in models[i:]]
            code = 5
            break
        except Exception as exc:  # фатальная ошибка: уже записанное остаётся, сводка — ниже
            print(f"Прогон {model.name} прерван: {type(exc).__name__}: {exc}", file=sys.stderr)
            if getattr(exc, "skip_model", False):
                skipped[model.name] = str(exc)[:200]
                silent.append(model.name)
                code = code if code in (3, 5) else 3
                continue
            code = 3
            break
        errors = report["summary"]["model_errors"]
        if errors and len(errors) == len(situations):
            print(f"Модель {model.name} не ответила ни на одну ситуацию", file=sys.stderr)
            silent.append(model.name)
            code = 3
            continue
        if errors:
            print(f"Модель {model.name} не ответила на {len(errors)} ситуаций", file=sys.stderr)
            code = code or 4
        report["comparison"] = run.compare(report, baseline)
        run.write_report(report, args.out_dir, blind=args.blind)
        reports.append(report)

    if not reports:
        print("Ни одна модель не ответила — сводка не пишется", file=sys.stderr)
        return 5 if budget_stopped else 3
    rows = [summary_row(r) for r in reports]
    table_rows = [{**r, "model": aliases[r["model"]]} for r in rows] if args.blind else rows
    first = reports[0]
    meta = {
        "label": label,
        "generated_on": first["generated_on"],
        "dataset_hash": first["dataset_hash"],
        "dataset_status": first["dataset_status"],
        "situations": first["summary"]["total"],
        "prompt": (first["results"][0].get("metrics") or {}).get("prompt", "—"),
        "not_answered": [aliases[n] if args.blind else n for n in silent],
        "incomplete": code == 3,
        "max_cost_usd": args.max_cost_usd,
        "spent_usd": round(budget.spent_usd, 6),
        "budget_stopped": budget_stopped,
        "not_run": [aliases[n] if args.blind else n for n in not_run],
        "skipped": {aliases[n] if args.blind else n: why for n, why in skipped.items()},
        # Пояснение адаптера к сводке (например, продуктовый конвейер 0.5 вместо compare.v1).
        "report_note": " ".join(
            dict.fromkeys(n for m in models if (n := getattr(m, "report_note", None)))
        )
        or None,
    }
    summary: dict[str, Any] = {**meta, "notice": "Предварительно (АР-19)", "rows": rows}
    if args.blind:
        summary["blind_key"] = {alias: name for name, alias in aliases.items()}
    (args.out_dir / f"{label}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path = args.out_dir / f"{label}.md"
    md_path.write_text(render_table(table_rows, meta), encoding="utf-8")
    print(f"Сводка: {md_path}")
    print(f"Расходы прогона: ${budget.spent_usd:.4f} из ${args.max_cost_usd:.2f}")
    return code


if __name__ == "__main__":
    sys.exit(main())

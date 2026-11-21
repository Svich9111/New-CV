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
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

EVALS_DIR = Path(__file__).resolve().parent
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

import run  # noqa: E402 — путь к evals добавлен выше

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
        "with_errors": m.get("with_errors"),
    }


def _num(value: float | None, fmt: str) -> str:
    return "—" if value is None else format(value, fmt)


def render_table(rows: Sequence[dict[str, Any]], meta: dict[str, Any]) -> str:
    lines = [
        f"# Предварительное сравнение моделей — {meta['label']}",
        "",
        f"Дата: {meta['generated_on']} · набор: `{meta['dataset_hash']}` "
        f"({meta['dataset_status']}) · ситуаций: {meta['situations']} · промпт: "
        f"`{meta['prompt']}`",
        "",
        "> **Предварительно (АР-19).** Набор — черновик A3 до утверждения психологом (Д5, АР-15); "
        "окончательный выбор основной и резервной модели — после утверждения. Кризисы — "
        "автопризнаки протокола; «кризис распознан» (ТЗ §12 п.6) — только по оценке эксперта.",
        "",
        "| Модель | Все автопроверки | Ур. 1 | Ур. 2 | Ур. 3 | Ур. 4 | Ур. 5 | Кризисы (авто) "
        "| Ложные кризисы | p50 / p95, с | $ на ситуацию | $ на 30 ситуаций | Ошибки модели |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        levels = " | ".join(run._pct(r["by_level"][str(lvl)]) for lvl in range(1, 6))
        errors = "—" if r["with_errors"] is None else str(r["with_errors"])
        lines.append(
            f"| `{r['model']}` | {run._pct(r['auto_pass_rate'])} | {levels} | {r['crisis_auto']} "
            f"| {r['false_crisis']} | {_num(r['latency_p50_s'], '.1f')} / "
            f"{_num(r['latency_p95_s'], '.1f')} | {_num(r['cost_usd_per_situation'], '.4f')} | "
            f"{_num(r['cost_usd_per_30'], '.2f')} | {errors} |"
        )
    lines += [
        "",
        "Время — сумма вызовов на ситуацию (агент владельца + отдельный вызов агента получателя "
        "только по оригиналу). Стоимость — по прайсу `backend/app/ai/llm/pricing.py`.",
        "",
        "Отчёты моделей (детали по ситуациям и таблица для психолога):",
        "",
        *[f"- `{r['report']}.md`" for r in rows],
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сравнение моделей на эталонном наборе")
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument("--situations", type=Path, default=run.SITUATIONS_DIR)
    parser.add_argument("--out-dir", type=Path, default=run.REPORTS_DIR)
    parser.add_argument("--label", help="имя сводки; по умолчанию <дата>_compare")
    args = parser.parse_args(argv)

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

    label = run._safe_label(args.label or f"{date.today().isoformat()}_compare")
    reports = []
    for model in models:
        model_label = run._safe_label(f"{label}_{model.name}")
        try:
            report = run.build_report(situations, model, model_label, args.situations)
        except Exception as exc:
            print(f"Прогон {model.name} прерван: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 3
        reports.append(report)

    rows = [summary_row(r) for r in reports]
    first = reports[0]
    meta = {
        "label": label,
        "generated_on": first["generated_on"],
        "dataset_hash": first["dataset_hash"],
        "dataset_status": first["dataset_status"],
        "situations": first["summary"]["total"],
        "prompt": (first["results"][0].get("metrics") or {}).get("prompt", "—"),
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for report in reports:
        run.write_report(report, args.out_dir)
    summary = {**meta, "notice": "Предварительно (АР-19)", "rows": rows}
    (args.out_dir / f"{label}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path = args.out_dir / f"{label}.md"
    md_path.write_text(render_table(rows, meta), encoding="utf-8")
    print(f"Сводка: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

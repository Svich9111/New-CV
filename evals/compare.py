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
        "только по оригиналу). Стоимость — по прайсу `backend/app/ai/llm/pricing.py`, включая "
        "оплаченные неудачные вызовы (отказ, обрезанный ответ).",
        "",
        "Ограничение (ревью 0.4 MINOR-8): безопасность и адаптацию оценивает один вызов агента "
        "получателя, а не отдельный классификатор §6.2 п.3 (задача 3.2) — цифры «доставка» и "
        "«Кризисы (авто)» нельзя переносить на продуктовый конвейер.",
        "",
        "Отчёты моделей (детали по ситуациям и таблица для психолога):",
        "",
        *[f"- `{r['report']}.md`" for r in rows],
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Коды выхода: 0 — все модели ответили; 1 — ошибка набора; 3 — модель не загрузилась,
    прогон прерван фатальной ошибкой или какая-то модель не ответила ни на одну ситуацию;
    4 — какая-то модель ответила не на все ситуации. Отчёты уже прогнанных моделей пишутся
    сразу и не теряются при ошибке следующей (ревью 0.4 MINOR-6, A8 D-3)."""
    parser = argparse.ArgumentParser(description="Сравнение моделей на эталонном наборе")
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument("--situations", type=Path, default=run.SITUATIONS_DIR)
    parser.add_argument("--out-dir", type=Path, default=run.REPORTS_DIR)
    parser.add_argument("--label", help="имя сводки; по умолчанию <дата>_compare")
    parser.add_argument(
        "--blind", action="store_true", help="отчёты моделей для эксперта без имени модели"
    )
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
    # Слепой режим (ревью 0.4 MINOR-5, 0.3 R2-MINOR-4): модели — «model-A/B/C» в именах файлов и
    # в сводке md; соответствие — только в json сводки (blind_key).
    aliases = {m.name: f"model-{chr(ord('A') + i)}" for i, m in enumerate(models)}
    reports: list[dict[str, Any]] = []
    silent: list[str] = []  # модели, не ответившие ни на одну ситуацию
    code = 0
    for model in models:
        tag = aliases[model.name] if args.blind else model.name
        model_label = run._safe_label(f"{label}_{tag}")
        try:
            report = run.build_report(situations, model, model_label, args.situations)
        except Exception as exc:  # фатальная ошибка: уже записанное остаётся, сводка — ниже
            print(f"Прогон {model.name} прерван: {type(exc).__name__}: {exc}", file=sys.stderr)
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
        run.write_report(report, args.out_dir, blind=args.blind)
        reports.append(report)

    if not reports:
        print("Ни одна модель не ответила — сводка не пишется", file=sys.stderr)
        return 3
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
    return code


if __name__ == "__main__":
    sys.exit(main())

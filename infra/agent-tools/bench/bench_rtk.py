"""Использование: bench_rtk.py <каталог для выводов>

Сравнение вывода команд: как есть vs. после переписывания хуком RTK.

Команда переписывается ровно так, как это сделает хук Claude Code (`rtk hook claude`),
поэтому замер отражает реальное поведение в сессии, а не ручной вызов `rtk ...`.
"""
import json
import subprocess
import sys

REPO = str(__import__("pathlib").Path(__file__).resolve().parents[3])
CMDS = [
    ("git status", "git status"),
    ("git log -n 20", "git log -n 20"),
    ("git diff (коммит 0.7b)", "git diff a49c5bf~1 a49c5bf"),
    ("git show --stat", "git show --stat a49c5bf"),
    ("ls -la backend/app", "ls -la backend/app"),
    ("find *.py", "find backend -name '*.py' -not -path '*/.venv/*'"),
    ("grep -rn user_id", "grep -rn user_id backend/app --include=*.py"),
    ("pytest (бэкенд, 1 падение)", "cd backend && uv run --locked pytest -q"),
    ("pytest -v", "cd backend && uv run --locked pytest -v"),
    ("ruff check", "cd backend && uv run --locked ruff check ."),
    ("mypy app", "cd backend && uv run --locked mypy app"),
    ("vitest (npm test)", "cd frontend && npm test"),
    ("eslint (npm run lint)", "cd frontend && npm run lint"),
    ("tsc --noEmit", "cd frontend && npx tsc --noEmit"),
    ("cat docs/TZ.md", "cat docs/TZ.md"),
]


def rewrite(cmd: str) -> str:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    out = subprocess.run(["rtk", "hook", "claude"], input=payload, capture_output=True, text=True).stdout
    if not out.strip():
        return cmd
    return json.loads(out)["hookSpecificOutput"]["updatedInput"]["command"]


def run(cmd: str) -> tuple[bytes, int]:
    p = subprocess.run(["bash", "-c", cmd], cwd=REPO, capture_output=True, timeout=600)
    return p.stdout + p.stderr, p.returncode


rows = []
for name, cmd in CMDS:
    new = rewrite(cmd)
    raw, rc_raw = run(cmd)
    if new == cmd:
        comp, rc_comp = raw, rc_raw
    else:
        comp, rc_comp = run(new)
    rows.append((name, cmd, new, len(raw), len(comp), rc_raw, rc_comp))
    with open(f"{sys.argv[1]}/{name.split()[0]}_{len(rows)}.raw", "wb") as f:
        f.write(raw)
    with open(f"{sys.argv[1]}/{name.split()[0]}_{len(rows)}.rtk", "wb") as f:
        f.write(comp)
    print(f"{name:32} {len(raw):>8} -> {len(comp):>7}  rc {rc_raw}/{rc_comp}  | {new}", flush=True)

json.dump(rows, open(f"{sys.argv[1]}/rtk_results.json", "w"), ensure_ascii=False, indent=1)
tr = sum(r[3] for r in rows)
tc = sum(r[4] for r in rows)
print(f"ИТОГО {tr} -> {tc} байт, экономия {100 * (1 - tc / tr):.1f}%")

"""Сравнение ответов Serena с grep + Read. Номера строк — на состояние 2026-11-23.

Использование: serena_baseline.py <serena_results.json>
"""
import json, subprocess, sys
R = str(__import__("pathlib").Path(__file__).resolve().parents[3])
def sh(c): return subprocess.run(["bash","-c",c],cwd=R,capture_output=True).stdout
def read(path, a=1, b=None):  # формат вывода Read: номер строки + таб
    lines = open(f"{R}/{path}", encoding="utf-8").read().splitlines()[a-1:b]
    return "".join(f"{i:>6}\t{l}\n" for i, l in enumerate(lines, a)).encode()
g = lambda c: len(sh(c))
base = {
 "A": (g("grep -rn complete_onboarding backend/app")+len(read("backend/app/repositories/users.py"))+len(read("backend/app/api/auth.py")),
       g("grep -rn complete_onboarding backend/app")+len(read("backend/app/repositories/users.py",51,85))+len(read("backend/app/api/auth.py",50,63))),
 "B": (len(read("backend/app/models/tables.py")), g("grep -nE '^(class|def|[A-Z][A-Za-z]+ =)' backend/app/models/tables.py")),
 "C": (len(read("backend/app/models/tables.py")), g("grep -n 'class PartnerMessage' backend/app/models/tables.py")+len(read("backend/app/models/tables.py",279,331))),
 "D": (len(read("backend/tests/test_rls_a8.py")), g("grep -rn test_dedicated_login_role_cannot_escape_to_superuser backend/tests")+len(read("backend/tests/test_rls_a8.py",1058,1082))),
 "E": (g("grep -rnw User backend/app backend/tests --include=*.py"),)*2,
 "F": (len(read("frontend/src/App.tsx")),)*2,
}
res = json.load(open(sys.argv[1]))
ser = {}
for x in res: ser[x["id"][0]] = ser.get(x["id"][0], 0) + x["bytes"]
names = {"A":"функция + кто вызывает","B":"обзор модуля tables.py (454 стр.)","C":"тело класса PartnerMessage","D":"один тест из файла 1274 стр.","E":"где используется User","F":"TS-компонент App (8 стр.)"}
tn=tc=ts=0
print(f"{'сценарий':36} {'наивно':>8} {'аккуратно':>10} {'Serena':>8}  экон. vs наивно / vs аккуратно")
for k in "ABCDEF":
    n,c = base[k]; s = ser[k]; tn+=n; tc+=c; ts+=s
    print(f"{k} {names[k]:34} {n:>8} {c:>10} {s:>8}  {100*(1-s/n):6.0f}% / {100*(1-s/c):6.0f}%")
print(f"ИТОГО {'':30} {tn:>8} {tc:>10} {ts:>8}  {100*(1-ts/tn):6.0f}% / {100*(1-ts/tc):6.0f}%")

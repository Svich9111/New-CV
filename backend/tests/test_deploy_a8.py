"""0.8 (A8): независимая проверка комплекта тестового стенда без Docker-демона.

ТЗ §7 (копии ≤ 30 дней), §9.1, §10 (резервное копирование, проверка восстановления); АР-10, АР-33,
АР-41; карточка docs/tasks/0.8.md; ревью docs/reviews/0.8-review.md.

Проверяется:
- ``infra/deploy/docker-compose.prod.yml`` статически (YAML): порты только у caddy, PostgreSQL и
  Redis — только внутренняя сеть, health checks и порядок запуска, ``:?`` у секретов, пароль
  суперпользователя — только у postgres и db-init; точка входа образа (env-guard) — настоящим ``sh``;
  ``docker compose config`` (только CLI, если он есть) — отказ без каждой обязательной переменной;
- ``gen_secrets.py``: права 600/700, ничего секретного в выводе, повторный запуск не меняет
  значения, ``--admin-token``;
- ``backup.sh`` и ``restore-check.sh`` — на ВРЕМЕННЫХ базах тестового кластера (5432 / scram):
  вместо ``docker compose exec -T postgres`` подставляется прослойка ``docker`` (PATH), которая
  запускает ту же команду локально; имя базы ``feelings`` прослойка заменяет на временную.
  Роли кластера общие (CLAUDE.md): ``restore-check.sh`` копируется во временный каталог, имя
  владельца ``feelings_owner`` в копии заменяется временной ролью ``a8d_<hex>_owner`` (для
  ``init-owner.sql`` — штатная переменная ``owner_role``); роль и базы удаляются после теста;
- АР-33: при ``staging``/``production`` без проверенного списка контактов API не стартует.

Данные — выдуманные. Сеть не используется."""

# ruff: noqa: S603, S607, S108, S103
# Фиксированные команды теста (psql, pg_dump, pg_restore, bash, git, docker, python3) с аргументами
# из теста; /tmp — сокет тестового кластера; 0o755 — исполняемая прослойка во временном каталоге.

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

from app.config import ConfigError, Settings
from app.help import HELP_CONTACTS, HelpContacts, check_help_contacts_config
from tests.conftest import TEST_DATABASE_URL

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "infra" / "deploy"
COMPOSE_FILE = DEPLOY / "docker-compose.prod.yml"
GEN = DEPLOY / "gen_secrets.py"
BACKUP = DEPLOY / "backup.sh"
RESTORE = DEPLOY / "restore-check.sh"
INIT_OWNER = ROOT / "infra" / "db" / "init-owner.sql"
EXAMPLE = DEPLOY / ".env.prod.example"

LONG_LIVED = (
    "caddy", "api", "worker-memory", "worker-kb", "worker-pipeline", "worker-notify", "postgres",
    "redis",
)  # fmt: skip
SECRET_VARS = (
    "FEELINGS_OWNER_DB_PASSWORD",
    "FEELINGS_API_DB_PASSWORD",
    "FEELINGS_WORKER_DB_PASSWORD",
    "FEELINGS_ADMIN_DB_PASSWORD",
    "FEELINGS_TELEGRAM_BOT_TOKEN",
    "FEELINGS_TELEGRAM_WEBHOOK_SECRET",
    "FEELINGS_SESSION_SECRET",
    "FEELINGS_ENCRYPTION_KEYS",
    "FEELINGS_MEMORY_FINGERPRINT_KEY",
    "OPENROUTER_API_KEY",
)
GENERATED = (
    "FEELINGS_OWNER_DB_PASSWORD",
    "FEELINGS_API_DB_PASSWORD",
    "FEELINGS_WORKER_DB_PASSWORD",
    "FEELINGS_ADMIN_DB_PASSWORD",
    "FEELINGS_TELEGRAM_WEBHOOK_SECRET",
    "FEELINGS_SESSION_SECRET",
    "FEELINGS_ENCRYPTION_KEYS",
    "FEELINGS_MEMORY_FINGERPRINT_KEY",
)
MANUAL = {
    "FEELINGS_DOMAIN": "localhost",
    "FEELINGS_TLS": "internal",
    "FEELINGS_TELEGRAM_BOT_TOKEN": "123456789:AAFakeA8TokenForStaticCheck_xxxxxxxxxx",
    "FEELINGS_BOT_USERNAME": "a8_fake_bot",
    "OPENROUTER_API_KEY": "sk-or-a8-fake",
}


# =========================================================================== compose (статически)


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def _profiles(svc: dict) -> list[str]:
    return list(svc.get("profiles") or [])


def test_only_caddy_publishes_ports_80_443(compose: dict) -> None:
    """Карточка: наружу только 80/443; у БД, Redis, API и воркеров портов нет."""
    with_ports = {n: s["ports"] for n, s in compose["services"].items() if s.get("ports")}
    assert set(with_ports) == {"caddy"}
    targets = {str(p).rsplit(":", 1)[-1] for p in with_ports["caddy"]}
    assert targets <= {"80", "443", "443/udp"}
    for name, svc in compose["services"].items():
        assert "expose" not in svc or name == "caddy", name
        assert svc.get("network_mode") != "host", name


def test_postgres_redis_only_internal_network(compose: dict) -> None:
    nets = compose["networks"]
    assert nets["data"].get("internal") is True
    for name in ("postgres", "redis"):
        assert compose["services"][name]["networks"] == ["data"], name
    # Разовые операции с БД — тоже только во внутренней сети.
    for name in ("db-init", "migrate", "db-roles"):
        assert compose["services"][name]["networks"] == ["data"], name
    # Caddy не видит БД.
    assert "data" not in compose["services"]["caddy"]["networks"]


def test_healthchecks_and_startup_order(compose: dict) -> None:
    """Карточка: health checks у всех долгоживущих сервисов; запуск по готовности."""
    svcs = compose["services"]
    for name in LONG_LIVED:
        svc = svcs[name]
        assert not _profiles(svc), name
        hc = svc.get("healthcheck")
        assert hc and hc.get("test"), name
        for dep, cond in (svc.get("depends_on") or {}).items():
            assert cond.get("condition") == "service_healthy", (name, dep)
    assert set(svcs["caddy"]["depends_on"]) == {"api"}
    assert set(svcs["api"]["depends_on"]) == {"postgres", "redis"}
    long_lived_found = {n for n, s in svcs.items() if not _profiles(s)}
    assert long_lived_found == set(LONG_LIVED)  # новый сервис — сюда и в health checks


def test_secrets_required_and_never_literal() -> None:
    """Карточка: секретов и заглушек в Git нет; обязательные без значения — отказ (``:?``)."""
    raw = COMPOSE_FILE.read_text(encoding="utf-8")
    for var in SECRET_VARS:
        uses = re.findall(r"\$\{" + var + r"(:[?-][^}]*)?\}", raw)
        assert uses, var
        assert any(u.startswith(":?") for u in uses), var
        assert not any(u.startswith(":-") and u != ":-" for u in uses), var
    # Нет литеральных паролей/токенов в значениях окружения.
    for m in re.finditer(r"^\s*(\w*(PASSWORD|SECRET|TOKEN|_KEY|KEYS))\s*:\s*(.+)$", raw, re.M):
        value = m.group(3).strip()
        assert value.startswith(("${", "*", "/run/secrets/")), m.group(0)


def test_superuser_password_only_for_postgres_and_db_init(compose: dict) -> None:
    """Карточка: пароль суперпользователя — отдельно (docker secret), только postgres и db-init."""
    users = {n for n, s in compose["services"].items() if s.get("secrets")}
    assert users == {"postgres", "db-init"}
    raw = COMPOSE_FILE.read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD:" not in raw
    assert "POSTGRES_PASSWORD_FILE: /run/secrets/postgres_superuser_password" in raw


def test_migrations_under_owner_app_under_api_role(compose: dict) -> None:
    """АР-10, АР-41: migrate — под feelings_owner; API — feelings_api; воркеры и админ — свои роли
    входа; суперпользователь нигде, кроме db-init."""
    svcs = compose["services"]
    assert "feelings_owner:" in svcs["migrate"]["environment"]["FEELINGS_MIGRATION_DATABASE_URL"]
    assert "//feelings_api:" in svcs["api"]["environment"]["FEELINGS_DATABASE_URL"]
    assert "//feelings_admin_login:" in svcs["api"]["environment"]["FEELINGS_ADMIN_DATABASE_URL"]
    for w in ("worker-memory", "worker-kb", "worker-pipeline", "worker-notify"):
        assert "//feelings_worker_login:" in svcs[w]["environment"]["FEELINGS_WORKER_DATABASE_URL"]
    assert svcs["db-roles"]["environment"]["PGUSER"] == "feelings_owner"
    for name, svc in svcs.items():
        env = json.dumps(svc.get("environment") or {})
        if name not in ("db-init", "postgres"):
            assert "//postgres:" not in env and '"PGUSER": "postgres"' not in env, name


def _guard(compose: dict) -> list[str]:
    ep = compose["x-backend"]["entrypoint"]
    assert ep[0] == "/bin/sh" and ep[1] == "-c"
    return [ep[0], ep[1], ep[2].replace("$$", "$"), ep[3]]


@pytest.mark.parametrize(
    ("env", "ok"),
    [
        ("staging", True),
        ("production", True),
        ("dev", False),
        ("local", False),
        ("test", False),
        ("", False),
        ("Staging", False),
        ("staging ", False),
        ("staging;production", False),
        ("*", False),
    ],
)
def test_entrypoint_env_guard(compose: dict, env: str, ok: bool) -> None:
    """DEPLOY §0, §12 (ревью MINOR-8): точка входа пропускает только staging/production — иначе
    код 64 до запуска процесса."""
    r = subprocess.run(
        [*_guard(compose), "echo", "STARTED"],
        env={"PATH": os.environ["PATH"], "FEELINGS_ENV": env},
        capture_output=True,
        text=True,
        check=False,
    )
    if ok:
        assert (r.returncode, r.stdout.strip()) == (0, "STARTED")
    else:
        assert r.returncode == 64 and "STARTED" not in r.stdout
        assert "допустимы только staging и production" in r.stderr


def test_all_backend_services_use_guarded_entrypoint(compose: dict) -> None:
    for name in (
        "api",
        "worker-memory",
        "worker-kb",
        "worker-pipeline",
        "worker-notify",
        "migrate",
    ):
        svc = compose["services"][name]
        assert svc["entrypoint"] == compose["x-backend"]["entrypoint"], name
        assert svc.get("read_only") is True and svc.get("cap_drop") == ["ALL"], name


def test_example_has_no_secret_values_and_secrets_ignored() -> None:
    """Секретов в Git нет: шаблон пуст, рабочие файлы под .gitignore."""
    values = {}
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        values[k.strip()] = v.strip()
    for var in (*SECRET_VARS, "FEELINGS_ADMIN_TOKEN_SHA256", "FEELINGS_DOMAIN", "FEELINGS_TLS"):
        assert values.get(var, "") == "", var
    assert values["FEELINGS_ENV"] == "staging"
    paths = [
        "infra/deploy/.env",
        "infra/deploy/secrets/postgres_superuser_password",
        "infra/deploy/admin-token.txt",
        "infra/deploy/backups/feelings-x.dump",
    ]
    r = subprocess.run(
        ["git", "check-ignore", *paths], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert sorted(r.stdout.split()) == sorted(paths)
    tracked = subprocess.run(
        ["git", "ls-files", "infra/deploy"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    assert not [p for p in tracked if p.endswith((".env", ".dump")) or "secrets/" in p]


def test_caddy_no_access_log_and_error_log_filtered() -> None:
    """ТЗ §9.1 п.5, ревью MINOR-1: журнал доступа не ведётся; в журнале ошибок удалены адрес
    (токен приглашения) и заголовки (секрет вебхука)."""
    caddy = (DEPLOY / "Caddyfile").read_text(encoding="utf-8")
    body = "\n".join(line.split("#", 1)[0] for line in caddy.splitlines())
    assert "request>uri delete" in body and "request>headers delete" in body
    site = body[body.index("{$FEELINGS_DOMAIN}") :]
    assert not re.search(r"^\s*log(\s|$)", site, re.M)  # нет журнала доступа у сайта


# =========================================================================== docker compose config


DOCKER = shutil.which("docker")


def _compose_config(
    env_file: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FEELINGS_", "OPENROUTER"))}
    env |= extra_env or {}
    return subprocess.run(
        [DOCKER or "docker", "compose", "-p", "a8static", "-f", str(COMPOSE_FILE), "--env-file",
         str(env_file), "--profile", "ops", "config", "--quiet"],
        env=env, capture_output=True, text=True, check=False,
    )  # fmt: skip


def _gen(tmp: Path, *args: str, umask: int | None = None) -> subprocess.CompletedProcess:
    def pre() -> None:
        if umask is not None:
            os.umask(umask)

    return subprocess.run(
        ["python3", "-I", str(GEN), "--dir", str(tmp), *args],
        capture_output=True,
        text=True,
        check=False,
        preexec_fn=pre,
    )


def _read_env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _set_env(path: Path, values: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8")
    for k, v in values.items():
        text, n = re.subn(rf"^{k}=.*$", f"{k}={v}", text, flags=re.M)
        if not n:
            text += f"{k}={v}\n"
    path.write_text(text, encoding="utf-8")


@pytest.mark.skipif(DOCKER is None, reason="нет docker CLI — проверка только статически")
def test_compose_refuses_without_each_required_variable(tmp_path: Path) -> None:
    """Карточка: обязательные переменные без значения — отказ запуска (``docker compose config``
    со сгенерированными секретами; каждая обязательная — по очереди пустая)."""
    _gen(tmp_path)
    env = tmp_path / ".env"
    _set_env(env, MANUAL | {"FEELINGS_SECRETS_DIR": str(tmp_path / "secrets")})
    ok = _compose_config(env)
    assert ok.returncode == 0, ok.stderr
    required = (*SECRET_VARS, "FEELINGS_DOMAIN", "FEELINGS_TLS", "FEELINGS_BOT_USERNAME",
                "FEELINGS_ENV", "FEELINGS_IMAGE_TAG", "FEELINGS_ENCRYPTION_KEY_ID")  # fmt: skip
    for var in required:
        broken = tmp_path / f"{var}.env"
        broken.write_text(env.read_text(encoding="utf-8"), encoding="utf-8")
        _set_env(broken, {var: ""})
        r = _compose_config(broken)
        assert r.returncode != 0 and var in r.stderr, var


# =========================================================================== gen_secrets.py


def test_gen_secrets_permissions_and_quiet_output(tmp_path: Path) -> None:
    """Секреты — только на сервере: .env 600, secrets/ 700, файл суперпользователя 600 (и при
    umask 000); в выводе — только имена переменных."""
    r = _gen(tmp_path, umask=0)
    assert r.returncode == 1  # ручные переменные не заданы
    env = tmp_path / ".env"
    su = tmp_path / "secrets" / "postgres_superuser_password"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "secrets").stat().st_mode) == 0o700
    assert stat.S_IMODE(su.stat().st_mode) == 0o600
    values = _read_env(env)
    out = r.stdout + r.stderr
    for name in GENERATED:
        assert values[name], name
        assert values[name] not in out, name
        assert f"заполнено: {name}" in out
    assert su.read_text().strip() not in out
    assert len(su.read_text().strip()) >= 32
    assert "заполнено: FEELINGS_ADMIN_TOKEN_SHA256" not in out
    assert not (tmp_path / "admin-token.txt").exists()
    assert values["FEELINGS_ADMIN_TOKEN_SHA256"] == ""  # админ-API закрыт по умолчанию
    # Пароли подставляются в URL без кодирования (шаблон: только A-Z a-z 0-9 _ -).
    for name in GENERATED[:4]:
        assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", values[name]), name
    assert len({values[n] for n in GENERATED}) == len(GENERATED)


def test_gen_secrets_rerun_keeps_values_and_fixes_mode(tmp_path: Path) -> None:
    _gen(tmp_path)
    env = tmp_path / ".env"
    su = tmp_path / "secrets" / "postgres_superuser_password"
    first, su_first = _read_env(env), su.read_text()
    _set_env(
        env, MANUAL | {"FEELINGS_SESSION_SECRET": "a8-operator-chosen-session-secret-xxxxxxxxxxxx"}
    )
    os.chmod(env, 0o644)
    r = _gen(tmp_path)
    assert r.returncode == 0, r.stdout
    assert "права .env исправлены на 600" in r.stdout
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    second = _read_env(env)
    for name in GENERATED:
        if name != "FEELINGS_SESSION_SECRET":
            assert second[name] == first[name], name
    assert second["FEELINGS_SESSION_SECRET"] == "a8-operator-chosen-session-secret-xxxxxxxxxxxx"
    assert su.read_text() == su_first
    assert "заполнено" not in r.stdout
    for v in second.values():
        if len(v) >= 16:
            assert v not in r.stdout


def test_gen_secrets_admin_token_only_by_flag(tmp_path: Path) -> None:
    r = _gen(tmp_path, "--admin-token")
    token_file = tmp_path / "admin-token.txt"
    token = token_file.read_text().strip()
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert token not in r.stdout + r.stderr
    assert (
        _read_env(tmp_path / ".env")["FEELINGS_ADMIN_TOKEN_SHA256"]
        == hashlib.sha256(token.encode()).hexdigest()
    )
    # Повтор с флагом при существующем хеше — ничего нового.
    r2 = _gen(tmp_path, "--admin-token")
    assert token_file.read_text().strip() == token
    assert "заполнено: FEELINGS_ADMIN_TOKEN_SHA256" not in r2.stdout


# =========================================================================== АР-33


@pytest.mark.parametrize("env", ["staging", "production"])
def test_staging_refuses_unverified_help_contacts(env: str) -> None:
    """АР-33, DEPLOY §0: стенд (staging) без проверенного списка контактов помощи не стартует."""
    settings = Settings(env=env)
    unverified = HelpContacts(contacts=HELP_CONTACTS.contacts, verified_by_operator=False)
    with pytest.raises(ConfigError, match="АР-33"):
        check_help_contacts_config(settings, unverified)
    check_help_contacts_config(
        settings, HelpContacts(contacts=HELP_CONTACTS.contacts, verified_by_operator=True)
    )


# =========================================================================== backup.sh / restore-check.sh


SHIM = r"""#!/usr/bin/env bash
# A8: вместо «docker compose … exec -T postgres КОМАНДА…» — та же команда на тестовом кластере.
args=("$@")
i=0
while (( i < ${#args[@]} )) && [[ "${args[$i]}" != exec ]]; do i=$((i + 1)); done
if (( i >= ${#args[@]} )); then echo "shim: не exec: $*" >&2; exit 99; fi
i=$((i + 1))
[[ "${args[$i]}" == -T ]] && i=$((i + 1))
[[ "${args[$i]}" == postgres ]] || { echo "shim: не postgres: ${args[$i]}" >&2; exit 98; }
i=$((i + 1))
cmd=()
for a in "${args[@]:$i}"; do
    [[ "$a" == feelings ]] && a="$A8_DB"
    cmd+=("$a")
done
printf '%s\n' "${cmd[*]}" >> "$A8_SHIM_LOG"
case "${A8_SHIM_MODE:-}" in
    fail_dump) [[ "${cmd[0]}" == pg_dump ]] && { echo "pg_dump: сбой" >&2; exit 1; } ;;
    garbage_dump) [[ "${cmd[0]}" == pg_dump ]] && { echo "не архив"; exit 0; } ;;
    slow_dump) [[ "${cmd[0]}" == pg_dump ]] && sleep "${A8_SLOW:-2}" ;;
esac
exec "${cmd[@]}"
"""


@dataclass
class Cluster:
    env: dict[str, str]

    def psql(self, db: str, sql: str, *, check: bool = True) -> str:
        r = subprocess.run(
            ["psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", db,
             "-c", sql],
            env=self.env, capture_output=True, text=True, check=False,
        )  # fmt: skip
        if check:
            assert r.returncode == 0, r.stderr
        return r.stdout.strip()


@pytest.fixture(scope="module")
def cluster() -> Cluster:
    u = urlsplit(TEST_DATABASE_URL.replace("+asyncpg", ""))
    host = "/tmp" if u.hostname in ("localhost", "127.0.0.1") else (u.hostname or "/tmp")
    env = {
        "PATH": os.environ["PATH"],
        "PGHOST": host,
        "PGPORT": str(u.port or 5432),
        "PGUSER": "postgres",
        "PGPASSWORD": u.password or "",
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": "C.UTF-8",
    }
    c = Cluster(env)
    if subprocess.run(["psql", "-X", "-Atc", "SELECT 1", "-d", "postgres"], env=env,
                      capture_output=True, check=False).returncode:  # fmt: skip
        # Вход по паролю через сокет может не подойти (scram на TCP) — пробуем TCP.
        env["PGHOST"] = u.hostname or "localhost"
    assert c.psql("postgres", "SELECT 1") == "1"
    return c


@pytest.fixture
def shim(tmp_path: Path, cluster: Cluster) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(SHIM, encoding="utf-8")
    os.chmod(bindir / "docker", 0o755)
    env = dict(cluster.env)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["A8_SHIM_LOG"] = str(tmp_path / "shim.log")
    env["FEELINGS_BACKUP_DIR"] = str(tmp_path / "backups")
    return env


@pytest.fixture
def source_db(cluster: Cluster) -> Iterator[str]:
    db = f"a8d_{uuid.uuid4().hex[:10]}_src"
    cluster.psql("postgres", f"CREATE DATABASE {db}")
    cluster.psql(db, "CREATE TABLE a8_rows (id int PRIMARY KEY, body text)")
    cluster.psql(
        db, "INSERT INTO a8_rows SELECT g, 'выдуманный-' || g FROM generate_series(1, 50) g"
    )
    yield db
    cluster.psql("postgres", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def _run(
    script: Path, env: dict[str, str], *args: str, timeout: float = 120
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args], env=env, capture_output=True, text=True, check=False,
        timeout=timeout,
    )  # fmt: skip


def _dumps(d: Path) -> list[Path]:
    return sorted(d.glob("feelings-*.dump")) if d.exists() else []


def test_backup_creates_private_readable_dump(
    shim: dict[str, str], source_db: str, cluster: Cluster
) -> None:
    """ТЗ §10: копия создаётся, права 600 в каталоге 700, архив читается; ``.partial`` не
    остаётся; вывод — без секретов."""
    env = shim | {"A8_DB": source_db}
    r = _run(BACKUP, env)
    assert r.returncode == 0, r.stderr
    bdir = Path(env["FEELINGS_BACKUP_DIR"])
    dumps = _dumps(bdir)
    assert len(dumps) == 1
    assert re.fullmatch(r"feelings-\d{4}-\d\d-\d\d-\d{6}-\d{9}\.dump", dumps[0].name)
    assert stat.S_IMODE(dumps[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(bdir.stat().st_mode) == 0o700
    assert not list(bdir.glob("*.partial"))
    listing = subprocess.run(
        ["pg_restore", "--list", str(dumps[0])], capture_output=True, text=True, check=True
    )
    assert "a8_rows" in listing.stdout
    assert "выдуманный" not in r.stdout + r.stderr


@pytest.mark.parametrize("mode", ["fail_dump", "garbage_dump"])
def test_backup_failure_leaves_no_copy(shim: dict[str, str], source_db: str, mode: str) -> None:
    """Сбой pg_dump или нечитаемый архив — копия не засчитывается: файлов нет, код ≠ 0."""
    env = shim | {"A8_DB": source_db, "A8_SHIM_MODE": mode}
    r = _run(BACKUP, env)
    assert r.returncode != 0
    bdir = Path(env["FEELINGS_BACKUP_DIR"])
    assert _dumps(bdir) == [] and not list(bdir.glob("*.partial"))


def _aged(path: Path, seconds: float) -> None:
    path.write_bytes(b"old")
    t = time.time() - seconds
    os.utime(path, (t, t))


@pytest.mark.parametrize(
    ("keep", "kept", "deleted"),
    [
        (None, ["28d21h", "1d"], ["28d23h", "31d", "45d"]),
        ("30", ["28d21h", "1d"], ["28d23h", "31d", "45d"]),
        ("7", ["1d", "5d21h"], ["5d23h", "28d21h"]),
        ("2", ["21h"], ["23h", "1d"]),
    ],
)
def test_backup_retention_under_30_days(
    shim: dict[str, str], source_db: str, keep: str | None, kept: list[str], deleted: list[str]
) -> None:
    """ТЗ §7, §10 (ревью MINOR-3): копия удаляется, когда ей больше (KEEP−1) суток без 2 часов —
    ни одна не доживает до 30 суток; чужие файлы каталога не трогаются."""
    env = shim | {"A8_DB": source_db}
    if keep is not None:
        env["FEELINGS_BACKUP_KEEP_DAYS"] = keep
    bdir = Path(env["FEELINGS_BACKUP_DIR"])
    bdir.mkdir(mode=0o700)

    def secs(tag: str) -> float:
        m = re.fullmatch(r"(?:(\d+)d)?(?:(\d+)h)?", tag)
        assert m
        return int(m.group(1) or 0) * 86400 + int(m.group(2) or 0) * 3600

    for tag in (*kept, *deleted):
        _aged(bdir / f"feelings-old-{tag}.dump", secs(tag))
    _aged(bdir / "other-45d.dump", 45 * 86400)
    r = _run(BACKUP, env)
    assert r.returncode == 0, r.stderr
    names = {p.name for p in bdir.iterdir()}
    for tag in kept:
        assert f"feelings-old-{tag}.dump" in names, tag
    for tag in deleted:
        assert f"feelings-old-{tag}.dump" not in names, tag
    assert "other-45d.dump" in names


@pytest.mark.parametrize("keep", ["0", "1", "31", "100", "-1", "abc", "3.5", " 30", "1e1"])
def test_backup_rejects_bad_keep_days(shim: dict[str, str], source_db: str, keep: str) -> None:
    env = shim | {"A8_DB": source_db, "FEELINGS_BACKUP_KEEP_DAYS": keep}
    r = _run(BACKUP, env)
    assert r.returncode == 64, (keep, r.stderr)
    assert _dumps(Path(env["FEELINGS_BACKUP_DIR"])) == []
    assert not Path(env["A8_SHIM_LOG"]).exists()  # pg_dump даже не запускался


def test_backup_runs_serialized_by_lock(shim: dict[str, str], source_db: str) -> None:
    """Ревью MINOR-3(а): одновременные запуски не перезаписывают друг друга — идут по очереди."""
    env = shim | {"A8_DB": source_db, "A8_SHIM_MODE": "slow_dump", "A8_SLOW": "1.5"}
    t0 = time.monotonic()
    procs = [subprocess.Popen(["bash", str(BACKUP)], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(3)]  # fmt: skip
    codes = [p.wait(timeout=120) for p in procs]
    elapsed = time.monotonic() - t0
    assert codes == [0, 0, 0]
    assert len(_dumps(Path(env["FEELINGS_BACKUP_DIR"]))) == 3
    assert elapsed >= 4.0  # три снятия по 1,5 с — последовательно


# --- restore-check.sh: копия скрипта с временным владельцем (роли кластера общие)


@dataclass
class OwnerEnv:
    role: str
    script: Path
    env: dict[str, str]


@pytest.fixture
def owner_env(tmp_path: Path, shim: dict[str, str], cluster: Cluster) -> Iterator[OwnerEnv]:
    role = f"a8d_{uuid.uuid4().hex[:10]}_owner"
    cluster.psql("postgres", f"CREATE ROLE {role} NOLOGIN CREATEROLE")
    script_dir = tmp_path / "deploy"
    script_dir.mkdir()
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    shutil.copy(INIT_OWNER, db_dir / "init-owner.sql")
    text = RESTORE.read_text(encoding="utf-8")
    assert text.count("-v keep_owner_password=1") == 1
    text = text.replace(
        "-v keep_owner_password=1", f"-v keep_owner_password=1 -v owner_role={role}"
    )
    text = text.replace("'feelings_owner'", f"'{role}'")
    script = script_dir / "restore-check.sh"
    script.write_text(text, encoding="utf-8")
    yield OwnerEnv(role, script, shim)
    for db in cluster.psql(
        "postgres", f"SELECT datname FROM pg_database WHERE datdba = '{role}'::regrole"
    ).split():
        cluster.psql("postgres", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
    for db in cluster.psql(
        "postgres", "SELECT datname FROM pg_database WHERE datname LIKE 'feelings_restore_check_%'"
    ).split():
        cluster.psql("postgres", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
    # Базы с зависимостями от роли (в том числе брошенные упавшим прогоном) — только временные.
    for db in cluster.psql(
        "postgres",
        "SELECT DISTINCT d.datname FROM pg_shdepend s JOIN pg_database d ON d.oid = s.dbid "
        f"WHERE s.refobjid = '{role}'::regrole",
    ).split():
        if db.startswith(("a8d_", "feelings_restore_check_")):
            cluster.psql("postgres", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
    cluster.psql("postgres", f"DROP OWNED BY {role}; DROP ROLE IF EXISTS {role}")


def _good_source(cluster: Cluster, owner: str) -> str:
    """База «как стенд»: схема во владении владельца, RLS, vector, alembic_version, права
    feelings_app — через функцию; ничего лишнего."""
    db = f"a8d_{uuid.uuid4().hex[:10]}_src"
    cluster.psql("postgres", f"CREATE DATABASE {db} OWNER {owner}")
    cluster.psql(db, "CREATE EXTENSION IF NOT EXISTS vector")
    cluster.psql(db, f"""
        SET ROLE {owner};
        CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('0013');
        CREATE TABLE users (id int PRIMARY KEY, name text);
        INSERT INTO users VALUES (1, 'Выдуманная Анна');
        ALTER TABLE users ENABLE ROW LEVEL SECURITY;
        ALTER TABLE users FORCE ROW LEVEL SECURITY;
        CREATE POLICY users_self ON users FOR SELECT TO feelings_app USING (id = 1);
        GRANT SELECT ON users TO feelings_app;
    """)  # fmt: skip
    return db


def _dump(cluster: Cluster, db: str, path: Path) -> Path:
    subprocess.run(["pg_dump", "-Fc", "-U", "postgres", "-d", db, "-f", str(path)],
                   env=cluster.env, check=True, capture_output=True)  # fmt: skip
    cluster.psql("postgres", f"DROP DATABASE {db} WITH (FORCE)")
    return path


def _restore_dbs(cluster: Cluster) -> list[str]:
    return cluster.psql(
        "postgres", "SELECT datname FROM pg_database WHERE datname LIKE 'feelings_restore_check_%'"
    ).split()


def test_restore_check_good_copy_passes_and_temp_db_dropped(
    tmp_path: Path, owner_env: OwnerEnv, cluster: Cluster
) -> None:
    """Контроль: копия базы «как стенд» восстанавливается тем же порядком (init-owner → pg_restore),
    проверки проходят, временная база удалена; печатаются только имена таблиц и числа."""
    before = _restore_dbs(cluster)
    dump = _dump(cluster, _good_source(cluster, owner_env.role), tmp_path / "good.dump")
    r = _run(owner_env.script, owner_env.env, str(dump))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "users: 1" in r.stdout and "схема: 0013" in r.stdout
    assert "Анна" not in r.stdout + r.stderr
    assert _restore_dbs(cluster) == before


WEAKENINGS = {
    "table_owned_by_superuser": "RESET ROLE; CREATE TABLE a8_evil (x int);",
    "function_owned_by_superuser": (
        "RESET ROLE; CREATE FUNCTION a8_evil() RETURNS int LANGUAGE sql SECURITY DEFINER AS 'select 1';"
    ),
    "schema_create_for_app": "GRANT CREATE ON SCHEMA public TO feelings_app;",
    "no_schema_version": "DROP TABLE alembic_version;",
    "no_rls_anywhere": "ALTER TABLE users DISABLE ROW LEVEL SECURITY;",
}
# Ослабления, которые восстановление переносит, а restore-check.sh не замечает (дефект A8-0.8-D2).
UNDETECTED = {
    "rls_disabled_on_one_table": (
        "CREATE TABLE couples (id int PRIMARY KEY); ALTER TABLE couples ENABLE ROW LEVEL SECURITY;"
        " ALTER TABLE users DISABLE ROW LEVEL SECURITY;"
    ),
    "policies_dropped": (
        "CREATE TABLE couples (id int PRIMARY KEY); ALTER TABLE couples ENABLE ROW LEVEL SECURITY;"
        " DROP POLICY users_self ON users;"
    ),
    "direct_table_grant_to_login_role": "GRANT SELECT ON users TO feelings_api;",
}


@pytest.mark.parametrize("case", sorted(WEAKENINGS))
def test_restore_check_fails_on_weakened_copy(
    tmp_path: Path, owner_env: OwnerEnv, cluster: Cluster, case: str
) -> None:
    """Подложенная ослабленная копия — проверка восстановления падает, временная база удалена."""
    before = _restore_dbs(cluster)
    src = _good_source(cluster, owner_env.role)
    cluster.psql(src, f"SET ROLE {owner_env.role}; {WEAKENINGS[case]}")
    dump = _dump(cluster, src, tmp_path / f"{case}.dump")
    r = _run(owner_env.script, owner_env.env, str(dump))
    assert r.returncode != 0, r.stdout
    assert "восстановление проверено" not in r.stdout
    assert _restore_dbs(cluster) == before


@pytest.mark.parametrize("case", sorted(UNDETECTED))
def test_restore_check_detects_rls_and_grant_weakening(
    tmp_path: Path, owner_env: OwnerEnv, cluster: Cluster, case: str
) -> None:
    src = _good_source(cluster, owner_env.role)
    cluster.psql(src, f"SET ROLE {owner_env.role}; {UNDETECTED[case]}")
    dump = _dump(cluster, src, tmp_path / f"{case}.dump")
    r = _run(owner_env.script, owner_env.env, str(dump))
    assert r.returncode != 0, r.stdout


def test_restore_check_db_mode_fails_on_weak_database_rights(
    owner_env: OwnerEnv, cluster: Cluster
) -> None:
    """``--db`` (после восстановления по DEPLOY §10): база с TEMPORARY у PUBLIC (как после
    pg_restore без init-owner, ревью MAJOR-1) и с EXECUTE функций vector у PUBLIC — отказ; после
    init-owner — проходит."""
    db = _good_source(cluster, owner_env.role)
    try:
        r = _run(owner_env.script, owner_env.env, "--db", db)
        assert r.returncode != 0 and "права базы ослаблены" in r.stderr, r.stderr
        cluster.psql(db, f"REVOKE TEMPORARY, CREATE ON DATABASE {db} FROM PUBLIC")
        r = _run(owner_env.script, owner_env.env, "--db", db)
        assert r.returncode != 0 and "функции vector" in r.stderr, r.stderr
        init = subprocess.run(
            ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-v", "keep_owner_password=1",
             "-v", f"owner_role={owner_env.role}", "-U", "postgres", "-d", db,
             "-f", str(INIT_OWNER)],
            env=cluster.env, capture_output=True, text=True, check=False,
        )  # fmt: skip
        assert init.returncode == 0, init.stderr
        r = _run(owner_env.script, owner_env.env, "--db", db)
        assert r.returncode == 0, r.stderr
    finally:
        cluster.psql("postgres", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


@pytest.mark.parametrize("name", ["feelings;drop", "Feelings", "a b", "../x", ""])
def test_restore_check_db_name_validated(shim: dict[str, str], name: str) -> None:
    r = _run(RESTORE, shim, "--db", name)
    assert r.returncode != 0
    assert not Path(shim["A8_SHIM_LOG"]).exists()


def test_restore_check_without_copies_fails(shim: dict[str, str]) -> None:
    Path(shim["FEELINGS_BACKUP_DIR"]).mkdir()
    r = _run(RESTORE, shim)
    assert r.returncode == 1 and "нет копии" in r.stderr


def test_backup_partial_file_private_while_dumping(shim: dict[str, str], source_db: str) -> None:
    """Пока pg_dump пишет копию (личные данные, шифротексты), файл уже недоступен другим (umask)."""
    env = shim | {"A8_DB": source_db, "A8_SHIM_MODE": "slow_dump", "A8_SLOW": "2"}
    proc = subprocess.Popen(["bash", str(BACKUP)], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)  # fmt: skip
    bdir = Path(env["FEELINGS_BACKUP_DIR"])
    seen: list[int] = []
    end = time.monotonic() + 10
    while proc.poll() is None and time.monotonic() < end:
        seen += [stat.S_IMODE(p.stat().st_mode) for p in bdir.glob("*.partial") if p.exists()]
        if seen:
            break
        time.sleep(0.05)
    assert proc.wait(timeout=60) == 0
    assert seen and all(m & 0o077 == 0 for m in seen), [oct(m) for m in seen]

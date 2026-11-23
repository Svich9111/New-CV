"""A8: независимая проверка ``infra/db/roles.sql`` (Б-69, Б-75; АР-10; CLAUDE.md «ключи и токены»).

Скрипт запускается настоящим ``psql`` против тестовой БД. Роли кластера общие (CLAUDE.md): тест
создаёт только свои временные роли ``a8r_<hex>_*`` и удаляет их в ``finally`` с проверкой, что
не осталось ни одной; групповые роли ``feelings_*`` (атрибуты, хеши паролей, членства без
временных ролей) сверяются снимком до и после. Пароль ``feelings_api`` меняется только в
``dry_run`` (откат). Данные не создаются.

Совпадение пароля проверяется по SCRAM-хешу в ``pg_authid`` (вычисление StoredKey из пароля) —
это работает и на кластере с входом ``trust``; настоящий вход идёт с паролем (``PGPASSWORD`` в
окружении psql, не в командной строке), поэтому тесты проходят и на кластере ``scram-sha-256``.

Журнал сервера для проверок «пароль не попал в журнал»: ``A8_SERVER_LOG`` или файл, найденный по
настройкам сервера (``pg_current_logfile()``, иначе ``<data_directory>/log``), и обязательно
подтверждённый меткой ``RAISE LOG``. Если журнал фактически недоступен (другая машина, нет прав) —
такие тесты пропускаются с явной причиной, а не проходят вхолостую.

Путь к скрипту можно подменить переменной ``A8_ROLES_SQL`` (мутационная проверка копий скрипта)."""

import base64
import hashlib
import hmac
import os
import re
import secrets
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from tests.conftest import TEST_DATABASE_URL

ROLES_SQL = Path(
    os.environ.get("A8_ROLES_SQL")
    or Path(__file__).resolve().parents[2] / "infra" / "db" / "roles.sql"
)
# Полный путь к psql (не «частичный» путь процесса, S607); нет psql — явная ошибка в тесте.
PSQL = shutil.which("psql")
GROUPS = ("feelings_app", "feelings_worker", "feelings_admin", "feelings_api")

pytestmark = pytest.mark.usefixtures("migrated_db")


# ------------------------------------------------------------------ помощники


@dataclass(frozen=True)
class Conn:
    """Подключение psql: адрес без пароля и пароль отдельно (уходит в PGPASSWORD)."""

    url: str
    password: str | None


def _conn(user: str | None = None, password: str | None = None) -> Conn:
    """По умолчанию — суперпользователь тестовой БД (из TEST_DATABASE_URL); иначе — ``user``."""
    scheme, rest = TEST_DATABASE_URL.split("://", 1)
    parts = urlsplit(
        f"postgresql://{rest}" if scheme.startswith("postgresql") else TEST_DATABASE_URL
    )
    host = parts.netloc.split("@")[-1]
    if user is None:
        user, password = parts.username or "postgres", parts.password
    return Conn(urlunsplit(parts._replace(netloc=f"{user}@{host}")), password)


def _env(conn: Conn) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FEELINGS_", "PG"))}
    if conn.password is not None:
        env["PGPASSWORD"] = conn.password
    return env


def _psql() -> str:
    assert PSQL, "нужен клиент psql ≥ 15 в PATH"
    return PSQL


def sql(query: str, *, conn: Conn | None = None, check: bool = True) -> str:
    """Запрос через psql (по умолчанию — под суперпользователем тестовой БД)."""
    conn = conn or _conn()
    done = subprocess.run(  # noqa: S603 — полный путь к psql, аргументы из теста
        [_psql(), conn.url, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-c", query],
        env=_env(conn),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if check:
        assert done.returncode == 0, done.stderr
    return done.stdout.strip() if done.returncode == 0 else "ERROR: " + done.stderr


def run(
    env: dict[str, str],
    worker: str,
    admin: str,
    *,
    dry_run: bool = False,
    allow_logged: bool = False,
    conn: Conn | None = None,
    pgoptions: str | None = None,
) -> subprocess.CompletedProcess[str]:
    conn = conn or _conn()
    args = [_psql(), conn.url, "-X", "-v", f"worker_login={worker}", "-v", f"admin_login={admin}"]
    if dry_run:
        args += ["-v", "dry_run=1"]
    if allow_logged:
        args += ["-v", "allow_logged=1"]
    full_env = {**_env(conn), **env}
    if pgoptions:
        full_env["PGOPTIONS"] = pgoptions
    done = subprocess.run(  # noqa: S603 — полный путь к psql, аргументы из теста
        [*args, "-f", str(ROLES_SQL)],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    # Ни при каком исходе пароль не печатается (stdout, stderr).
    for value in env.values():
        if value:
            assert value not in done.stdout and value not in done.stderr
    return done


def pw() -> str:
    return secrets.token_urlsafe(32)


def scram_matches(verifier: str | None, password: str) -> bool:
    """Совпадает ли пароль с SCRAM-SHA-256-хешем из ``pg_authid.rolpassword`` (RFC 5802/7677)."""
    if not verifier:
        return False
    m = re.fullmatch(r"SCRAM-SHA-256\$(\d+):([^$]+)\$([^:]+):(.+)", verifier)
    assert m, "ожидается SCRAM-SHA-256"
    salted = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), base64.b64decode(m.group(2)), int(m.group(1))
    )
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    return hashlib.sha256(client_key).digest() == base64.b64decode(m.group(3))


def verifier(role: str) -> str | None:
    out = sql(f"SELECT coalesce(rolpassword, '') FROM pg_authid WHERE rolname = '{role}'")
    return out or None


def snapshot() -> str:
    """Общие роли: атрибуты, хеш пароля, членства и члены (без временных ролей тестов)."""
    return sql(
        "SELECT string_agg(format('%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s', r.rolname, r.rolsuper, "
        "r.rolinherit, r.rolcreaterole, r.rolcreatedb, r.rolcanlogin, r.rolreplication, "
        "r.rolbypassrls, md5(coalesce(a.rolpassword, '')), "
        "ARRAY(SELECT g.rolname || ':' || m.inherit_option || m.set_option || m.admin_option "
        "      FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid WHERE m.member = r.oid "
        "      ORDER BY 1), "
        "ARRAY(SELECT u.rolname || ':' || m.inherit_option || m.set_option || m.admin_option "
        "      FROM pg_auth_members m JOIN pg_roles u ON u.oid = m.member WHERE m.roleid = r.oid "
        "      AND u.rolname NOT LIKE 't\\_%' AND u.rolname NOT LIKE 'a8r\\_%' ORDER BY 1)), "
        "E'\\n' ORDER BY r.rolname) "
        "FROM pg_roles r JOIN pg_authid a ON a.oid = r.oid "
        "WHERE r.rolname LIKE 'feelings\\_%' OR r.rolname = 'postgres'"
    )


def settable(login: str) -> set[str]:
    """Роли, которыми ``login`` может стать (SET ROLE, с учётом вложенных членств)."""
    out = sql(
        f"SELECT rolname FROM pg_roles WHERE rolname <> '{login}' "
        f"AND pg_has_role('{login}', oid, 'SET')"
    )
    return set(out.split()) if out else set()


def inherited(login: str) -> set[str]:
    out = sql(
        f"SELECT rolname FROM pg_roles WHERE rolname <> '{login}' "
        f"AND pg_has_role('{login}', oid, 'USAGE')"
    )
    return set(out.split()) if out else set()


def set_role_as(login: str, password: str, role: str) -> bool:
    """Настоящий вход ``login`` (с паролем) и ``SET ROLE role``."""
    out = sql(f"SET ROLE {role}; SELECT current_user", conn=_conn(login, password), check=False)
    if out.startswith("ERROR"):
        assert "permission denied" in out, out
        return False
    return out == role


@dataclass
class Tag:
    tag: str

    def name(self, kind: str) -> str:
        return f"a8r_{self.tag}_{kind}"


@pytest.fixture
def tag() -> Iterator[Tag]:
    """Уникальный префикс временных ролей; после теста — ни одной роли с ним, общие не тронуты."""
    t = Tag(uuid.uuid4().hex[:10])
    before = snapshot()
    try:
        yield t
    finally:
        names = sql(
            f"SELECT string_agg(rolname, ' ') FROM pg_roles WHERE rolname LIKE 'a8r\\_{t.tag}\\_%'"
        ).split()
        # Управляющий (_mgr) — последним: он выдавал членства созданным им ролям (grantor).
        names.sort(key=lambda n: n.endswith("_mgr"))
        errors = []
        for n in names:
            sql(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {n}")
            out = sql(f'DROP ROLE "{n}"', check=False)
            if out.startswith("ERROR"):
                errors.append(out)
        assert not errors, errors
        assert sql(f"SELECT count(*) FROM pg_roles WHERE rolname LIKE 'a8r\\_{t.tag}\\_%'") == "0"
        assert snapshot() == before


def manager(t: Tag, *settings: str) -> Conn:
    """Роль-управляющий без суперпользователя (CREATEROLE + ADMIN OPTION на группы)."""
    name, password = t.name("mgr"), pw()
    sql(f"CREATE ROLE {name} LOGIN CREATEROLE PASSWORD '{password}'")
    sql(f"GRANT feelings_worker, feelings_app, feelings_admin TO {name} WITH ADMIN OPTION")
    for s in settings:
        sql(f"ALTER ROLE {name} SET {s}")
    return _conn(name, password)


_LOG: list[Path | str] = []


def _server_log() -> Path:
    """Файл журнала тестового сервера, подтверждённый меткой; иначе — явный пропуск теста."""
    if not _LOG:
        _LOG.append(_find_server_log())
    found = _LOG[0]
    if isinstance(found, str):
        pytest.skip(f"журнал сервера фактически недоступен: {found}")
    return found


def _find_server_log() -> Path | str:
    candidates: list[Path] = []
    if os.environ.get("A8_SERVER_LOG"):
        candidates.append(Path(os.environ["A8_SERVER_LOG"]))
    datadir = sql("SHOW data_directory")
    current = sql("SELECT coalesce(pg_current_logfile(), '')")
    if current:
        candidates.append(Path(current) if current.startswith("/") else Path(datadir) / current)
    candidates.append(Path(datadir) / "log")
    marker = f"a8-log-marker-{uuid.uuid4().hex}"
    sql(f"DO $$ BEGIN RAISE LOG '{marker}'; END $$")
    for path in candidates:
        if os.access(path, os.R_OK) and marker in path.read_text("utf-8", "replace"):
            return path
    tried = ", ".join(str(p) for p in candidates)
    return f"метка RAISE LOG не найдена в доступных файлах ({tried}); задайте A8_SERVER_LOG"


def log_offset() -> int:
    return _server_log().stat().st_size


def log_since(offset: int) -> str:
    with _server_log().open("rb") as f:
        f.seek(offset)
        return f.read().decode("utf-8", "replace")


# ------------------------------------------------------------------ состав ролей (АР-10)


def test_worker_and_admin_logins_reach_only_their_roles(tag: Tag) -> None:
    """Роль входа воркера может стать только feelings_worker и feelings_app, админа — только
    feelings_admin; сами ничего не наследуют (NOINHERIT, INHERIT FALSE); не суперпользователи;
    пароль — SCRAM и совпадает с заданным."""
    w, a, wp, ap = tag.name("w"), tag.name("a"), pw(), pw()
    done = run({"FEELINGS_WORKER_DB_PASSWORD": wp, "FEELINGS_ADMIN_DB_PASSWORD": ap}, w, a)
    assert done.returncode == 0, done.stderr
    assert settable(w) == {"feelings_worker", "feelings_app"}
    assert settable(a) == {"feelings_admin"}
    assert inherited(w) == set() and inherited(a) == set()
    attrs = sql(
        "SELECT string_agg(format('%s:%s%s%s%s%s%s%s', rolname, rolcanlogin, rolinherit, rolsuper, "
        f"rolcreaterole, rolcreatedb, rolreplication, rolbypassrls), ' ' ORDER BY rolname) "
        f"FROM pg_roles WHERE rolname IN ('{w}', '{a}')"
    )
    # format('%s', boolean) печатает t/f.
    assert attrs == f"{a}:tffffff {w}:tffffff"
    assert scram_matches(verifier(w), wp) and scram_matches(verifier(a), ap)
    assert not scram_matches(verifier(w), ap)
    # Настоящий вход: без SET ROLE — ни одного права; SET ROLE — только свои роли.
    out = sql("SELECT 1 FROM public.users LIMIT 1", conn=_conn(w, wp), check=False)
    assert "permission denied" in out
    assert set_role_as(w, wp, "feelings_worker") and set_role_as(w, wp, "feelings_app")
    for denied in ("feelings_admin", "feelings_api", "postgres", "pg_read_all_data", a):
        assert not set_role_as(w, wp, denied), denied
    assert set_role_as(a, ap, "feelings_admin")
    for denied in ("feelings_app", "feelings_worker", "feelings_api", "postgres", w):
        assert not set_role_as(a, ap, denied), denied


def test_api_login_only_app() -> None:
    """АР-10: feelings_api может стать только feelings_app и ничего не наследует."""
    assert settable("feelings_api") == {"feelings_app"}
    assert inherited("feelings_api") == set()
    assert (
        sql(
            "SELECT rolinherit::text || rolsuper::text FROM pg_roles WHERE rolname = 'feelings_api'"
        )
        == "falsefalse"
    )


def test_worker_claims_need_worker_role_not_app(tag: Tag) -> None:
    """Подтверждение к проверке A7 «0 или 1 захваченных задач»: значение — не предмет проверки, а
    граница прав. Функции захвата исполняет только feelings_worker; под feelings_app и без SET ROLE —
    permission denied. Предел 1 — не больше одной строки; транзакция откатывается."""
    w, wp = tag.name("w"), pw()
    assert run({"FEELINGS_WORKER_DB_PASSWORD": wp}, w, tag.name("a")).returncode == 0
    calls = (
        "memory_claim_extraction(interval '30 min', interval '10 min', 1)",
        "memory_claim_compaction(interval '1 hour', interval '10 min', 1)",
        "kb_claim_index(interval '10 min', 1)",
    )
    for call in calls:
        for prefix in ("", "SET LOCAL ROLE feelings_app; "):
            out = sql(
                f"BEGIN; {prefix}SELECT count(*) FROM {call}; ROLLBACK",
                conn=_conn(w, wp),
                check=False,
            )
            assert "permission denied" in out, (prefix, call, out)
        out = sql(
            f"BEGIN; SET LOCAL ROLE feelings_worker; SELECT count(*) FROM {call}; ROLLBACK",
            conn=_conn(w, wp),
        )
        assert out in ("0", "1"), out


# ------------------------------------------------------------------ повторный запуск, dry_run


def test_rerun_rotates_password_only_and_dry_run_changes_nothing(tag: Tag) -> None:
    w, a, wp1, ap1 = tag.name("w"), tag.name("a"), pw(), pw()
    assert (
        run(
            {"FEELINGS_WORKER_DB_PASSWORD": wp1, "FEELINGS_ADMIN_DB_PASSWORD": ap1}, w, a
        ).returncode
        == 0
    )
    members = sql(
        "SELECT string_agg(u.rolname || '>' || g.rolname || ':' || m.inherit_option || m.set_option "
        "|| m.admin_option, ' ' ORDER BY 1) FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid "
        f"JOIN pg_roles u ON u.oid = m.member WHERE u.rolname IN ('{w}', '{a}')"
    )
    # Повтор только с паролем воркера: воркер — новый пароль, админ не тронут, членства те же.
    wp2 = pw()
    assert run({"FEELINGS_WORKER_DB_PASSWORD": wp2}, w, a).returncode == 0
    assert scram_matches(verifier(w), wp2) and not scram_matches(verifier(w), wp1)
    assert scram_matches(verifier(a), ap1)
    after = sql(
        "SELECT string_agg(u.rolname || '>' || g.rolname || ':' || m.inherit_option || m.set_option "
        "|| m.admin_option, ' ' ORDER BY 1) FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid "
        f"JOIN pg_roles u ON u.oid = m.member WHERE u.rolname IN ('{w}', '{a}')"
    )
    assert after == members
    # dry_run: новый пароль обоих и новая роль — всё выполнено и проверено, но откатано.
    hashes = (verifier(w), verifier(a))
    snap = snapshot()
    done = run(
        {
            "FEELINGS_WORKER_DB_PASSWORD": pw(),
            "FEELINGS_ADMIN_DB_PASSWORD": pw(),
            "FEELINGS_API_DB_PASSWORD": pw(),
        },
        w,
        tag.name("a2"),
        dry_run=True,
    )
    assert done.returncode == 0, done.stderr
    assert "dry_run" in done.stdout
    assert (verifier(w), verifier(a)) == hashes
    assert sql(f"SELECT count(*) FROM pg_roles WHERE rolname = '{tag.name('a2')}'") == "0"
    assert snapshot() == snap  # пароль feelings_api тоже не изменился


# ------------------------------------------------------------------ плохой ввод


BAD_NAMES = [
    "x; DROP ROLE postgres; --",
    "x'; CREATE ROLE a8r_evil SUPERUSER; --",
    'x"; CREATE ROLE a8r_evil SUPERUSER; --',
    "two words",
    "",
    "Upper",
    "1digit",
    "a" * 64,
    "feelings_app",
    "feelings_worker",
    "feelings_admin",
    "feelings_api",
    "pg_monitor",
    "pg_a8r_new",
    "postgres",
    "x\nCREATE ROLE a8r_evil",
]


@pytest.mark.parametrize("bad", BAD_NAMES, ids=lambda s: repr(s)[:24])
@pytest.mark.parametrize("which", ["worker", "admin"])
def test_bad_names_refused_without_traces(tag: Tag, bad: str, which: str) -> None:
    password = pw()
    snap = snapshot()
    roles_before = sql("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'a8r\\_evil%'")
    good = tag.name("ok")
    worker, admin = (bad, good) if which == "worker" else (good, bad)
    env = {"FEELINGS_WORKER_DB_PASSWORD": password, "FEELINGS_ADMIN_DB_PASSWORD": pw()}
    done = run(env, worker, admin)
    assert done.returncode != 0
    assert snapshot() == snap
    assert sql(f"SELECT count(*) FROM pg_roles WHERE rolname = '{good}'") == "0"  # откат целиком
    assert sql("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'a8r\\_evil%'") == roles_before
    assert sql("SELECT rolsuper::text FROM pg_roles WHERE rolname = 'postgres'") == "true"


def test_same_worker_and_admin_name_refused(tag: Tag) -> None:
    n = tag.name("same")
    done = run({"FEELINGS_WORKER_DB_PASSWORD": pw(), "FEELINGS_ADMIN_DB_PASSWORD": pw()}, n, n)
    assert done.returncode != 0 and "различаться" in done.stderr
    assert sql(f"SELECT count(*) FROM pg_roles WHERE rolname = '{n}'") == "0"


BAD_PASSWORDS = [
    "a" * 31,
    "a" * 40 + "'",
    "a" * 40 + "'; ALTER ROLE postgres NOSUPERUSER; --",
    "a" * 40 + ";",
    "a" * 20 + " " + "a" * 20,
    "a" * 40 + "$",
    "a" * 40 + "@",
    "a" * 40 + "\n",
    "а" * 40,  # кириллица
]


@pytest.mark.parametrize("bad", BAD_PASSWORDS, ids=lambda s: repr(s)[-12:])
@pytest.mark.parametrize(
    "env_name",
    ["FEELINGS_WORKER_DB_PASSWORD", "FEELINGS_ADMIN_DB_PASSWORD", "FEELINGS_API_DB_PASSWORD"],
)
def test_bad_passwords_refused_without_traces(tag: Tag, bad: str, env_name: str) -> None:
    snap = snapshot()
    w, a = tag.name("w"), tag.name("a")
    env = {"FEELINGS_WORKER_DB_PASSWORD": pw(), "FEELINGS_ADMIN_DB_PASSWORD": pw()}
    env[env_name] = bad
    done = run(env, w, a)
    assert done.returncode != 0
    assert env_name in done.stderr
    assert snapshot() == snap
    assert sql(f"SELECT count(*) FROM pg_roles WHERE rolname IN ('{w}', '{a}')") == "0"


def test_password_boundary_32_accepted(tag: Tag) -> None:
    p = "Aa0_-" * 6 + "zz"
    assert len(p) == 32
    w = tag.name("w")
    assert run({"FEELINGS_WORKER_DB_PASSWORD": p}, w, tag.name("a")).returncode == 0
    assert scram_matches(verifier(w), p)


def test_no_password_at_all_refused(tag: Tag) -> None:
    snap = snapshot()
    done = run(
        {"FEELINGS_WORKER_DB_PASSWORD": "", "FEELINGS_ADMIN_DB_PASSWORD": ""},
        tag.name("w"),
        tag.name("a"),
    )
    assert done.returncode != 0 and "не задан ни один пароль" in done.stderr
    assert snapshot() == snap


# ------------------------------------------------------------------ лишние членства и особые права


def test_nested_extra_membership_refused_with_rollback(tag: Tag) -> None:
    """Существующая роль входа админа через промежуточную группу может стать feelings_app —
    скрипт останавливается; весь запуск откатывается (новая роль воркера не создана, пароль и
    членства существующей роли прежние)."""
    g, a, w = tag.name("g"), tag.name("a"), tag.name("w")
    sql(f"CREATE ROLE {g} NOLOGIN IN ROLE feelings_app")
    sql(f"CREATE ROLE {a} NOLOGIN NOINHERIT IN ROLE {g}")
    done = run({"FEELINGS_WORKER_DB_PASSWORD": pw(), "FEELINGS_ADMIN_DB_PASSWORD": pw()}, w, a)
    assert (
        done.returncode != 0 and "лишними ролями" in done.stderr and "feelings_app" in done.stderr
    )
    assert sql(f"SELECT count(*) FROM pg_roles WHERE rolname = '{w}'") == "0"
    assert (
        sql(
            f"SELECT rolcanlogin::text || coalesce(rolpassword, '-') FROM pg_authid WHERE rolname = '{a}'"
        )
        == "false-"
    )
    assert settable(a) == {g, "feelings_app"}


@pytest.mark.parametrize(
    "attr", ["SUPERUSER", "CREATEROLE", "CREATEDB", "REPLICATION", "BYPASSRLS"]
)
def test_existing_privileged_role_untouched(tag: Tag, attr: str) -> None:
    w = tag.name("w")
    sql(f"CREATE ROLE {w} NOLOGIN {attr}")
    before = sql(f"SELECT row(r.*)::text FROM pg_authid r WHERE rolname = '{w}'")
    done = run({"FEELINGS_WORKER_DB_PASSWORD": pw()}, w, tag.name("a"))
    assert done.returncode != 0 and "особые права" in done.stderr
    assert sql(f"SELECT row(r.*)::text FROM pg_authid r WHERE rolname = '{w}'") == before


def test_login_role_with_own_members_refused(tag: Tag) -> None:
    x, m = tag.name("x"), tag.name("m")
    sql(f"CREATE ROLE {x} NOLOGIN")
    sql(f"CREATE ROLE {m} LOGIN IN ROLE {x}")
    done = run({"FEELINGS_WORKER_DB_PASSWORD": pw()}, x, tag.name("a"))
    assert "feelings_app" not in settable(m)
    # Отказ именно из-за членов роли (ревью 0.8, MINOR-4), а не по другой причине.
    assert done.returncode != 0 and "есть члены" in done.stderr and m in done.stderr, done.stderr


def test_login_role_with_direct_table_privileges_refused(tag: Tag) -> None:
    a = tag.name("a")
    sql(f"CREATE ROLE {a} NOLOGIN")
    sql(f"GRANT SELECT ON public.users TO {a}")
    done = run({"FEELINGS_ADMIN_DB_PASSWORD": pw()}, tag.name("w"), a)
    assert done.returncode != 0 and "собственные права" in done.stderr, done.stderr


# ------------------------------------------------------------------ журналирование паролей


def test_superuser_log_statement_all_password_not_logged(tag: Tag) -> None:
    """Под суперпользователем при log_statement=all, log_min_duration_statement=0 (на сессии)
    пароли не попадают в журнал сервера — ни при успехе, ни при ошибке после ALTER ROLE."""
    off = log_offset()
    wp, ap = pw(), pw()
    opts = "-c log_statement=all -c log_min_duration_statement=0 -c log_min_error_statement=debug5"
    done = run({"FEELINGS_WORKER_DB_PASSWORD": wp, "FEELINGS_ADMIN_DB_PASSWORD": ap},
               tag.name("w"), tag.name("a"), pgoptions=opts)  # fmt: skip
    assert done.returncode == 0, done.stderr
    # Ошибка после смены пароля: лишнее членство у админа (проверка итога — после ALTER ROLE).
    sql(f"GRANT feelings_app TO {tag.name('a')}")
    wp2, ap2 = pw(), pw()
    failed = run({"FEELINGS_WORKER_DB_PASSWORD": wp2, "FEELINGS_ADMIN_DB_PASSWORD": ap2},
                 tag.name("w"), tag.name("a"), pgoptions=opts)  # fmt: skip
    assert failed.returncode != 0
    logged = log_since(off)
    assert "ALTER ROLE" not in logged or tag.name("w") not in logged
    for p in (wp, ap, wp2, ap2):
        assert p not in logged


@pytest.mark.parametrize(
    "opts", ["-c log_transaction_sample_rate=1", "-c log_min_duration_sample=0"]
)
def test_superuser_sampled_logging_password_not_logged(tag: Tag, opts: str) -> None:
    off = log_offset()
    p = pw()
    done = run(
        {"FEELINGS_WORKER_DB_PASSWORD": p},
        tag.name("w"),
        tag.name("a"),
        dry_run=True,
        pgoptions=opts,
    )
    assert done.returncode == 0, done.stderr
    assert p not in log_since(off)


@pytest.mark.parametrize(
    "setting",
    [
        "log_statement = 'ddl'",
        "log_statement = 'all'",
        "log_min_duration_statement = 0",
        # Выборочное журналирование (ревью 0.8, MINOR-4): без суперпользователя тоже отказ.
        "log_transaction_sample_rate = 1",
        "log_min_duration_sample = 0",
    ],
)
def test_non_superuser_logged_refused_without_allow(tag: Tag, setting: str) -> None:
    """Без суперпользователя при журналировании запросов — отказ до первого запроса с паролем
    (в журнале пароля нет, ролей не создано); с -v allow_logged=1 — продолжает с предупреждением."""
    url = manager(tag, setting)
    off = log_offset()
    p = pw()
    w, a = tag.name("w"), tag.name("a")
    refused = run({"FEELINGS_WORKER_DB_PASSWORD": p}, w, a, conn=url)
    assert refused.returncode != 0 and "журналируются" in refused.stderr
    assert p not in log_since(off)
    assert sql(f"SELECT count(*) FROM pg_roles WHERE rolname = '{w}'") == "0"
    allowed = run(
        {"FEELINGS_WORKER_DB_PASSWORD": pw()}, w, a, conn=url, dry_run=True, allow_logged=True
    )
    assert allowed.returncode == 0, allowed.stderr
    assert "allow_logged" in allowed.stdout


def test_non_superuser_log_statement_mod_refused(tag: Tag) -> None:
    url = manager(tag, "log_statement = 'mod'")
    off = log_offset()
    p = pw()
    done = run(
        {"FEELINGS_WORKER_DB_PASSWORD": p}, tag.name("w"), tag.name("a"), conn=url, dry_run=True
    )
    assert p not in log_since(off)
    assert done.returncode != 0


def test_non_superuser_runs_and_creates_unprivileged_roles(tag: Tag) -> None:
    """Управляющий без суперпользователя (журнал выключен): роли создаются, состав ролей тот же."""
    url = manager(tag)
    w, a = tag.name("w"), tag.name("a")
    done = run(
        {"FEELINGS_WORKER_DB_PASSWORD": pw(), "FEELINGS_ADMIN_DB_PASSWORD": pw()}, w, a, conn=url
    )
    assert done.returncode == 0, done.stderr
    assert settable(w) == {"feelings_worker", "feelings_app"} and settable(a) == {"feelings_admin"}
    assert inherited(w) == set() and inherited(a) == set()


def test_non_superuser_alter_failure_password_not_logged(tag: Tag) -> None:
    url = manager(tag)
    other = tag.name("w")
    sql(f"CREATE ROLE {other} NOLOGIN")  # создана суперпользователем — у управляющего нет ADMIN
    off = log_offset()
    p = pw()
    done = run({"FEELINGS_WORKER_DB_PASSWORD": p}, other, tag.name("a"), conn=url)
    assert done.returncode != 0
    assert p not in log_since(off)

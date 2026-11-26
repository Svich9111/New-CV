"""0.8 (ревью MAJOR-2, MAJOR-1, MINOR-9): ``infra/db/init-owner.sql`` и миграции под владельцем схемы.

Скрипт запускается настоящим ``psql`` суперпользователем тестового сервера (пароль — из
``TEST_DATABASE_URL``, только в ``PGPASSWORD``) во ВРЕМЕННОЙ отдельной базе ``a8o_<hex>_db``.
Роли кластера общие (CLAUDE.md): владелец — временная роль ``a8o_<hex>_owner`` (переменная psql
``owner_role``), после теста база удаляется, ``DROP OWNED BY`` снимает выданные ею членства, роль
удаляется; общие роли ``feelings_*`` сверяются снимком до и после (атрибуты, хеши паролей,
членства). Групповые роли уже есть (их создаёт ``migrated_db``) — скрипт только выдаёт временному
владельцу ADMIN на них, как при восстановлении копии на сервер с готовыми ролями.

Проверяется (АР-10, АР-35, АР-41):
- атрибуты владельца (LOGIN CREATEROLE, без SUPERUSER / CREATEDB / REPLICATION / BYPASSRLS);
- права уровня базы: TEMPORARY и CREATE отозваны у PUBLIC — в том числе у feelings_api/feelings_app
  настоящим входом (восстановление копии не должно ослаблять базу);
- функции vector не исполняет PUBLIC и feelings_app;
- журнал сеансов владельца: текст запроса не пишется и при ошибке (пароль в ``ALTER ROLE``);
- ``alembic upgrade head`` → ``downgrade base`` → ``upgrade head`` под владельцем; ни одного
  объекта приложения во владении суперпользователя; права совпадают с базой ``feelings_test``,
  мигрированной суперпользователем;
- отказ при привилегированной существующей роли владельца и при слабом пароле; пароль нигде не
  печатается и не попадает в журнал сервера (журнал — как в ``test_login_roles_a8.py``: при
  фактической недоступности тест журнала пропускается с причиной).

Работает на кластере с входом ``trust`` и ``scram-sha-256`` (вход владельца — с паролем)."""

import os
import re
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from tests.conftest import API_ROLE_PASSWORD, BACKEND_DIR, TEST_DATABASE_URL
from tests.test_login_roles_a8 import (
    Conn,
    _conn,
    _env,
    _psql,
    log_offset,
    log_since,
    pw,
    snapshot,
    sql,
)

INIT_OWNER_SQL = Path(
    os.environ.get("A8_INIT_OWNER_SQL")
    or Path(__file__).resolve().parents[2] / "infra" / "db" / "init-owner.sql"
)

pytestmark = pytest.mark.usefixtures("migrated_db")

# Роли приложения для проверок прав базы и функций vector; feelings_billing — роль платежей (0019).
APP_ROLES = (
    "public", "feelings_app", "feelings_api", "feelings_worker", "feelings_admin",
    "feelings_billing",
)  # fmt: skip

VECTOR_FUNCS = (
    "FROM pg_proc p JOIN pg_depend d ON d.classid = 'pg_proc'::regclass AND d.objid = p.oid "
    "AND d.deptype = 'e' JOIN pg_extension e ON e.oid = d.refobjid AND e.extname = 'vector'"
)
NOT_EXTENSION = "NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = {oid} AND d.deptype = 'e')"


def _db(conn: Conn, db: str) -> Conn:
    parts = urlsplit(conn.url)
    return Conn(urlunsplit(parts._replace(path=f"/{db}")), conn.password)


def _async_url(user: str, password: str, db: str) -> str:
    """Адрес SQLAlchemy (asyncpg) того же сервера под ``user`` в базе ``db``."""
    scheme, rest = TEST_DATABASE_URL.split("://", 1)
    host = rest.split("@", 1)[1].split("/", 1)[0]
    return f"{scheme}://{user}:{password}@{host}/{db}"


@dataclass
class Owner:
    name: str
    db: str
    password: str

    def conn(self) -> Conn:
        """Настоящий вход владельца (с паролем) во временную базу."""
        return _db(_conn(self.name, self.password), self.db)

    def su(self) -> Conn:
        """Суперпользователь тестового сервера во временной базе."""
        return _db(_conn(), self.db)


@pytest.fixture
def owner() -> Iterator[Owner]:
    tag = uuid.uuid4().hex[:10]
    o = Owner(f"a8o_{tag}_owner", f"a8o_{tag}_db", pw())
    before = snapshot()
    sql(f'CREATE DATABASE "{o.db}"')
    try:
        yield o
    finally:
        errors = []
        for q in (
            f'DROP DATABASE IF EXISTS "{o.db}" WITH (FORCE)',
            # Снимает и членства, выданные владельцем (GRANT feelings_app TO feelings_api в 0001).
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{o.name}') THEN "
            f"EXECUTE 'DROP OWNED BY \"{o.name}\"'; EXECUTE 'DROP ROLE \"{o.name}\"'; END IF; END $$",
        ):
            out = sql(q, check=False)
            if out.startswith("ERROR"):
                errors.append(out)
        assert not errors, errors
        assert sql(f"SELECT count(*) FROM pg_roles WHERE rolname LIKE 'a8o\\_{tag}\\_%'") == "0"
        assert sql(f"SELECT count(*) FROM pg_database WHERE datname = '{o.db}'") == "0"
        assert snapshot() == before


def run_init(
    o: Owner,
    *,
    password: str | None = None,
    keep_password: bool = False,
    pgoptions: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """init-owner.sql суперпользователем во временной базе; пароль владельца — только в окружении."""
    su = o.su()
    env = _env(su)
    secret = o.password if password is None else password
    if not keep_password:
        env["FEELINGS_OWNER_DB_PASSWORD"] = secret
    if pgoptions:
        env["PGOPTIONS"] = pgoptions
    args = [_psql(), su.url, "-X", "-v", f"owner_role={o.name}"]
    if keep_password:
        args += ["-v", "keep_owner_password=1"]
    done = subprocess.run(  # noqa: S603 — полный путь к psql, аргументы из теста
        [*args, "-f", str(INIT_OWNER_SQL)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if secret:
        assert secret not in done.stdout and secret not in done.stderr
    return done


def migrate(o: Owner, *args: str) -> None:
    """Alembic под владельцем: адрес с паролем — в окружении, не в командной строке."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FEELINGS_", "PG"))}
    env["FEELINGS_MIGRATION_DATABASE_URL"] = _async_url(o.name, o.password, o.db)
    done = subprocess.run(  # noqa: S603 — фиксированная команда, аргументы из теста
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert o.password not in done.stdout and o.password not in done.stderr
    assert done.returncode == 0, done.stderr[-4000:]


def acl_snapshot(conn: Conn, owner_name: str) -> str:
    """Права и владельцы объектов приложения в public (без членов расширений); имена владельца и
    суперпользователя заменены на OWNER — чтобы сравнить прогон под владельцем и суперпользователем.
    Пустой ACL (NULL) приводится к правам по умолчанию (acldefault): после цикла downgrade/upgrade
    ACL alembic_version становится явным, по смыслу тем же."""
    out = sql(
        "SELECT string_agg(line, E'\\n' ORDER BY line) FROM ("
        # Таблицы, представления, последовательности, индексы: владелец, ACL, RLS.
        " SELECT format('rel %s %s %s %s %s %s', c.relname, c.relkind, c.relowner::regrole, "
        "   coalesce(c.relacl, acldefault(CASE WHEN c.relkind = 'S' THEN 's' ELSE 'r' END::\"char\", c.relowner)), c.relrowsecurity, c.relforcerowsecurity) AS line FROM pg_class c "
        "  WHERE c.relnamespace = 'public'::regnamespace AND "
        + NOT_EXTENSION.format(oid="c.oid")
        + " UNION ALL"
        # Права на колонки.
        " SELECT format('col %s.%s %s', c.relname, a.attname, a.attacl) FROM pg_attribute a "
        "   JOIN pg_class c ON c.oid = a.attrelid "
        "  WHERE c.relnamespace = 'public'::regnamespace AND a.attacl IS NOT NULL "
        " UNION ALL"
        # Функции: владелец, ACL, SECURITY DEFINER, настройки.
        " SELECT format('fn %s %s %s %s %s', p.oid::regprocedure, p.proowner::regrole, "
        "   coalesce(p.proacl, acldefault('f', p.proowner)), "
        "   p.prosecdef, p.proconfig) FROM pg_proc p "
        "  WHERE p.pronamespace = 'public'::regnamespace AND "
        + NOT_EXTENSION.format(oid="p.oid")
        + " UNION ALL"
        # Типы приложения (перечисления).
        " SELECT format('type %s %s %s', t.typname, t.typowner::regrole, "
        "   coalesce(t.typacl, acldefault('T', t.typowner))) FROM pg_type t "
        "  WHERE t.typnamespace = 'public'::regnamespace AND t.typtype = 'e' "
        " UNION ALL"
        # Политики RLS.
        " SELECT format('pol %s.%s %s %s %s', c.relname, pl.polname, pl.polcmd, pl.polpermissive, "
        "   ARRAY(SELECT r::regrole::text FROM unnest(pl.polroles) r ORDER BY 1)) "
        "   FROM pg_policy pl JOIN pg_class c ON c.oid = pl.polrelid "
        " UNION ALL"
        " SELECT format('schema %s %s', n.nspowner::regrole, "
        "   coalesce(n.nspacl, acldefault('n', n.nspowner))) FROM pg_namespace n "
        "  WHERE n.nspname = 'public' "
        " UNION ALL"
        " SELECT format('db %s', coalesce(d.datacl, acldefault('d', d.datdba))) FROM pg_database d WHERE d.datname = current_database()"
        ") s",
        conn=conn,
    )
    return re.sub(rf"\b({re.escape(owner_name)}|postgres)\b", "OWNER", out)


def test_init_owner_and_migrations_under_owner(owner: Owner) -> None:
    o = owner
    done = run_init(o)
    assert done.returncode == 0, done.stderr
    assert "готовы" in done.stdout

    # Атрибуты владельца; пароль — SCRAM (вход проверяется ниже настоящим подключением).
    attrs = sql(
        "SELECT format('%s%s%s%s%s%s%s', rolcanlogin, rolcreaterole, rolinherit, rolsuper, "
        f"rolcreatedb, rolreplication, rolbypassrls) FROM pg_roles WHERE rolname = '{o.name}'"
    )
    assert attrs == "tttffff"
    assert (
        sql(f"SELECT rolpassword LIKE 'SCRAM-SHA-256$%' FROM pg_authid WHERE rolname = '{o.name}'")
        == "t"
    )
    assert sql(f"SELECT datdba::regrole::text FROM pg_database WHERE datname = '{o.db}'") == o.name

    # Права уровня базы (0001, АР-10): ни у кого из приложения нет TEMPORARY и CREATE.
    for role in APP_ROLES:
        privs = sql(
            f"SELECT has_database_privilege('{role}', '{o.db}', 'TEMPORARY')::text || "
            f"has_database_privilege('{role}', '{o.db}', 'CREATE')::text || "
            f"has_database_privilege('{role}', '{o.db}', 'CONNECT')::text"
        )
        assert privs == "falsefalsetrue", role

    # Функции vector: EXECUTE только у владельца (и суперпользователя-владельца расширения).
    total = int(sql(f"SELECT count(*) {VECTOR_FUNCS}", conn=o.su()))
    assert total > 0
    for role in APP_ROLES:
        n = sql(
            f"SELECT count(*) {VECTOR_FUNCS} WHERE has_function_privilege('{role}', p.oid, 'EXECUTE')",
            conn=o.su(),
        )
        assert n == "0", role
    assert sql(
        f"SELECT count(*) {VECTOR_FUNCS} WHERE has_function_privilege('{o.name}', p.oid, 'EXECUTE')",
        conn=o.su(),
    ) == str(total)

    # Сеанс владельца (настоящий вход): журнал текста запросов выключен на уровне роли.
    shown = sql(
        "SELECT current_setting('log_statement') || ' ' || current_setting('log_min_error_statement')"
        " || ' ' || current_setting('log_min_duration_statement')"
        " || ' ' || current_setting('log_min_duration_sample')"
        " || ' ' || current_setting('log_transaction_sample_rate')",
        conn=o.conn(),
    )
    assert shown == "none panic -1 -1 0"
    # Предзагрузка vector — только для владельца и только в этой базе (АР-41).
    preload = sql(
        "SELECT string_agg(coalesce(d.datname, '*') || ':' || c, ',') FROM pg_db_role_setting s "
        "LEFT JOIN pg_database d ON d.oid = s.setdatabase, unnest(s.setconfig) c "
        f"WHERE s.setrole = '{o.name}'::regrole AND c LIKE 'session_preload_libraries=%'"
    )
    assert preload == f"{o.db}:session_preload_libraries=vector"

    # Миграции под владельцем: вперёд, полный откат, снова вперёд.
    migrate(o, "upgrade", "head")
    migrate(o, "downgrade", "base")
    assert (
        sql(
            "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' "
            "AND relname <> 'alembic_version'",
            conn=o.su(),
        )
        == "0"
    )
    migrate(o, "upgrade", "head")
    head = sql("SELECT version_num FROM alembic_version", conn=o.su())
    assert head == sql("SELECT version_num FROM alembic_version")

    # Ни одного объекта приложения во владении суперпользователя (или кого-то, кроме владельца).
    foreign = sql(
        "SELECT string_agg(x, ', ') FROM ("
        " SELECT c.relname AS x FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace"
        f"  AND c.relowner::regrole::text <> '{o.name}' AND "
        + NOT_EXTENSION.format(oid="c.oid")
        + " UNION ALL SELECT p.oid::regprocedure::text FROM pg_proc p"
        f"  WHERE p.pronamespace = 'public'::regnamespace AND p.proowner::regrole::text <> '{o.name}'"
        "  AND "
        + NOT_EXTENSION.format(oid="p.oid")
        + " UNION ALL SELECT t.typname FROM pg_type t WHERE t.typnamespace = 'public'::regnamespace"
        f"  AND t.typtype = 'e' AND t.typowner::regrole::text <> '{o.name}') s",
        conn=o.su(),
    )
    assert foreign == "", foreign
    definers = int(
        sql(
            "SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND prosecdef",
            conn=o.su(),
        )
    )
    assert definers > 0

    # Права совпадают с прогоном миграций под суперпользователем (база feelings_test).
    as_owner, as_super = acl_snapshot(o.su(), o.name), acl_snapshot(_conn(), o.name)
    diff = sorted(set(as_owner.splitlines()) ^ set(as_super.splitlines()))
    assert as_owner == as_super, diff[:20]

    # feelings_api настоящим входом: временная таблица запрещена и до, и после SET ROLE.
    api = _db(_conn("feelings_api", API_ROLE_PASSWORD), o.db)
    for prefix in ("", "SET ROLE feelings_app; "):
        out = sql(f"{prefix}CREATE TEMP TABLE t_probe (x int)", conn=api, check=False)
        assert out.startswith("ERROR") and "permission denied" in out, out

    # Повторный запуск (без смены пароля — как restore-check.sh) безопасен; хеш пароля прежний.
    hash_before = sql(f"SELECT rolpassword FROM pg_authid WHERE rolname = '{o.name}'")
    again = run_init(o, keep_password=True)
    assert again.returncode == 0, again.stderr
    assert sql(f"SELECT rolpassword FROM pg_authid WHERE rolname = '{o.name}'") == hash_before


def test_init_owner_restores_database_privileges(owner: Owner) -> None:
    """MAJOR-1: права базы, ослабленные (как после pg_restore в новую базу без --create),
    повторный init возвращает; проверка находит ослабление."""
    o = owner
    assert run_init(o).returncode == 0
    sql(f'GRANT TEMPORARY ON DATABASE "{o.db}" TO PUBLIC', conn=o.su())
    assert sql(f"SELECT has_database_privilege('feelings_api', '{o.db}', 'TEMPORARY')") == "t"
    again = run_init(o, keep_password=True)
    assert again.returncode == 0, again.stderr
    assert sql(f"SELECT has_database_privilege('feelings_api', '{o.db}', 'TEMPORARY')") == "f"


@pytest.mark.parametrize("attr", ["SUPERUSER", "BYPASSRLS", "REPLICATION"])
def test_init_owner_refuses_privileged_existing_owner(owner: Owner, attr: str) -> None:
    o = owner
    sql(f'CREATE ROLE "{o.name}" NOLOGIN {attr}')
    before = sql(f"SELECT row(r.*)::text FROM pg_authid r WHERE rolname = '{o.name}'")
    done = run_init(o)
    assert done.returncode != 0 and "особые права" in done.stderr
    assert sql(f"SELECT row(r.*)::text FROM pg_authid r WHERE rolname = '{o.name}'") == before
    assert sql(f"SELECT datdba::regrole::text FROM pg_database WHERE datname = '{o.db}'") != o.name


@pytest.mark.parametrize(
    "bad", ["", "short", "x" * 31, "has space" + "x" * 32, "quote'" + "x" * 32]
)
def test_init_owner_refuses_weak_password(owner: Owner, bad: str) -> None:
    o = owner
    done = run_init(o, password=bad)
    assert done.returncode != 0 and "FEELINGS_OWNER_DB_PASSWORD" in done.stderr
    assert sql(f"SELECT count(*) FROM pg_roles WHERE rolname = '{o.name}'") == "0"


def test_init_owner_keep_password_needs_existing_role(owner: Owner) -> None:
    done = run_init(owner, keep_password=True)
    assert done.returncode != 0 and "keep_owner_password" in done.stderr
    assert sql(f"SELECT count(*) FROM pg_roles WHERE rolname = '{owner.name}'") == "0"


def test_init_owner_refuses_reserved_owner_name() -> None:
    """Имя владельца — не групповая роль и не pg_* (скрипт не трогает чужие роли)."""
    su = _conn()
    for name in ("feelings_app", "pg_monitor", "Bad-Name"):
        done = subprocess.run(  # noqa: S603 — полный путь к psql, аргументы из теста
            [_psql(), su.url, "-X", "-v", f"owner_role={name}", "-f", str(INIT_OWNER_SQL)],
            env={**_env(su), "FEELINGS_OWNER_DB_PASSWORD": pw()},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert done.returncode != 0 and "имя владельца" in done.stderr, name


def test_init_owner_password_not_logged(owner: Owner) -> None:
    """Пароль владельца не попадает в журнал сервера ни при init с log_statement=all (на сеансе),
    ни при ошибке запроса с паролем в сеансе самого владельца (log_min_error_statement роли)."""
    o = owner
    off = log_offset()
    opts = "-c log_statement=all -c log_min_duration_statement=0 -c log_min_error_statement=debug5"
    done = run_init(o, pgoptions=opts)
    assert done.returncode == 0, done.stderr
    # Ошибка запроса с «паролем» в сеансе владельца (как сбой ALTER ROLE … PASSWORD в db-roles):
    # на сервере log_min_error_statement по умолчанию error — текст попал бы в журнал.
    marker = pw()
    out = sql(
        f"ALTER ROLE a8o_absent_{uuid.uuid4().hex[:8]} PASSWORD '{marker}'",
        conn=o.conn(),
        check=False,
    )
    assert out.startswith("ERROR")
    # Сравнение: у суперпользователя (без настроек роли) тот же запрос журналируется — значит,
    # журнал и правда пишет текст запросов при ошибке, проверка не холостая.
    control = pw()
    sql(f"ALTER ROLE a8o_absent_{uuid.uuid4().hex[:8]} PASSWORD '{control}'", check=False)
    logged = log_since(off)
    assert o.password not in logged
    assert marker not in logged
    if sql("SELECT current_setting('log_min_error_statement')") != "panic":
        assert control in logged

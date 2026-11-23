"""Роли входа воркера и администратора из ``infra/db/roles.sql`` (Б-69, Б-75; АР-10).

Скрипт запускается настоящим ``psql`` против тестовой БД. Роли кластера общие (CLAUDE.md):
тест создаёт только свои временные роли входа с уникальными именами (не ``feelings_*``) и
удаляет их в ``finally``; групповые роли ``feelings_*`` и пароль ``feelings_api`` не меняются
(скрипт запускается без ``FEELINGS_API_DB_PASSWORD``, это проверяется). Проверки прав — в
откатываемых транзакциях. Данные не создаются."""

import os
import secrets
import subprocess
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import admin_scope, make_engine, make_sessionmaker, user_scope
from app.repositories.memory_work import worker_scope
from tests.conftest import TEST_DATABASE_URL

ROLES_SQL = Path(__file__).resolve().parents[2] / "infra" / "db" / "roles.sql"
GROUP_ROLES = ("feelings_app", "feelings_worker", "feelings_admin", "feelings_api")
PASSWORD_ENVS = ("FEELINGS_WORKER_DB_PASSWORD", "FEELINGS_ADMIN_DB_PASSWORD")


def _psql_url() -> str:
    scheme, rest = TEST_DATABASE_URL.split("://", 1)
    return f"postgresql://{rest}" if scheme.startswith("postgresql") else TEST_DATABASE_URL


def run_roles_sql(
    *,
    env: dict[str, str],
    worker: str,
    admin: str,
    dry_run: bool = False,
    allow_logged: bool = False,
    url: str | None = None,
) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("FEELINGS_", "PG"))}
    args = ["psql", url or _psql_url(), "-X"]
    args += ["-v", f"worker_login={worker}", "-v", f"admin_login={admin}"]
    if dry_run:
        args += ["-v", "dry_run=1"]
    if allow_logged:
        args += ["-v", "allow_logged=1"]
    return subprocess.run(  # noqa: S603 — фиксированная команда, аргументы из теста
        [*args, "-f", str(ROLES_SQL)],
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def login_url(login: str, password: str) -> str:
    parts = urlsplit(TEST_DATABASE_URL)
    host = parts.netloc.split("@")[-1]
    return urlunsplit(parts._replace(netloc=f"{login}:{quote(password, safe='')}@{host}"))


def new_password() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class LoginRoles:
    worker: str
    admin: str
    worker_password: str
    admin_password: str


async def _role_snapshot(engine: AsyncEngine) -> list[tuple[object, ...]]:
    """Групповые роли и их членства, а также хеш пароля feelings_api — для проверки «не тронуты»."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT r.rolname, r.rolcanlogin, r.rolinherit, a.rolpassword, "
                "ARRAY(SELECT g.rolname || ':' || m.inherit_option || ':' || m.set_option "
                "      FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid "
                "      WHERE m.member = r.oid ORDER BY 1), "
                "ARRAY(SELECT u.rolname FROM pg_auth_members m JOIN pg_roles u ON u.oid = m.member "
                "      WHERE m.roleid = r.oid AND u.rolname LIKE 'feelings\\_%' ORDER BY 1) "
                "FROM pg_roles r JOIN pg_authid a ON a.oid = r.oid "
                "WHERE r.rolname = ANY(:names) ORDER BY r.rolname"
            ),
            {"names": list(GROUP_ROLES)},
        )
        return [tuple(r) for r in rows]


async def _drop_roles(engine: AsyncEngine, *names: str) -> None:
    async with engine.connect() as conn:
        for name in names:
            await conn.execute(text(f'DROP ROLE IF EXISTS "{name}"'))
        await conn.commit()
        left = await conn.execute(
            text("SELECT count(*) FROM pg_roles WHERE rolname = ANY(:n)"), {"n": list(names)}
        )
        assert left.scalar_one() == 0


@pytest.fixture
async def login_roles(engine: AsyncEngine) -> AsyncIterator[LoginRoles]:
    tag = uuid.uuid4().hex[:10]
    roles = LoginRoles(f"t_wlogin_{tag}", f"t_alogin_{tag}", new_password(), new_password())
    before = await _role_snapshot(engine)
    try:
        done = run_roles_sql(
            env={
                "FEELINGS_WORKER_DB_PASSWORD": roles.worker_password,
                "FEELINGS_ADMIN_DB_PASSWORD": roles.admin_password,
            },
            worker=roles.worker,
            admin=roles.admin,
        )
        assert done.returncode == 0, done.stderr
        # Пароль не печатается ни в вывод, ни в ошибки.
        for out in (done.stdout, done.stderr):
            assert roles.worker_password not in out and roles.admin_password not in out
        yield roles
    finally:
        await _drop_roles(engine, roles.worker, roles.admin)
        assert await _role_snapshot(engine) == before  # feelings_* и пароль API не тронуты


async def _can_set_role(eng: AsyncEngine, role: str) -> bool:
    async with eng.connect() as conn:
        tx = await conn.begin()
        try:
            await conn.execute(text(f"SET LOCAL ROLE {role}"))
            current = (await conn.execute(text("SELECT current_user"))).scalar_one()
            return bool(current == role)
        except ProgrammingError as exc:
            assert "permission denied" in str(exc)
            return False
        finally:
            await tx.rollback()


async def test_created_roles_have_expected_attributes(
    engine: AsyncEngine, login_roles: LoginRoles
) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT r.rolname, r.rolcanlogin, r.rolinherit, r.rolsuper, r.rolcreaterole, "
                "r.rolcreatedb, r.rolreplication, r.rolbypassrls, a.rolpassword LIKE 'SCRAM-SHA-256$%', "
                "ARRAY(SELECT g.rolname || ':' || m.inherit_option || ':' || m.set_option || ':' "
                "      || m.admin_option FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid "
                "      WHERE m.member = r.oid ORDER BY 1) "
                "FROM pg_roles r JOIN pg_authid a ON a.oid = r.oid "
                "WHERE r.rolname = ANY(:n) ORDER BY r.rolname"
            ),
            {"n": [login_roles.worker, login_roles.admin]},
        )
        got = {r[0]: tuple(r[1:]) for r in rows}
    flags = (True, False, False, False, False, False, False, True)
    assert got[login_roles.admin] == (*flags, ["feelings_admin:false:true:false"])
    assert got[login_roles.worker] == (
        *flags,
        ["feelings_app:false:true:false", "feelings_worker:false:true:false"],
    )


async def test_worker_login_switches_only_to_worker_and_app(login_roles: LoginRoles) -> None:
    eng = make_engine(login_url(login_roles.worker, login_roles.worker_password))
    try:
        # NOINHERIT: без SET ROLE у роли входа нет ни одного права.
        async with eng.connect() as conn:
            assert (await conn.execute(text("SELECT current_user"))).scalar_one() == (
                login_roles.worker
            )
            with pytest.raises(ProgrammingError, match="permission denied"):
                await conn.execute(text("SELECT 1 FROM public.users LIMIT 1"))
            await conn.rollback()
        assert await _can_set_role(eng, "feelings_worker")
        assert await _can_set_role(eng, "feelings_app")
        for denied in ("feelings_admin", "feelings_api", "postgres"):
            assert not await _can_set_role(eng, denied), denied

        sm = make_sessionmaker(eng)
        # Те же области, что у воркеров (app.workers.memory / kb): одно подключение на обе.
        async with worker_scope(sm) as s:
            assert (await s.execute(text("SELECT current_user"))).scalar_one() == "feelings_worker"
        async with user_scope(sm, None) as s:
            assert (await s.execute(text("SELECT current_user"))).scalar_one() == "feelings_app"
        # Функции захвата работы доступны под feelings_worker (EXECUTE). Предел функции не меньше 1,
        # поэтому захват может выдать задачу чужого теста — транзакция откатывается, аренда не остаётся.
        async with eng.connect() as conn:
            tx = await conn.begin()
            await conn.execute(text("SET LOCAL ROLE feelings_worker"))
            for sql, params in (
                (
                    "SELECT count(*) FROM memory_claim_extraction(:g, :l, 1)",
                    {"g": timedelta(minutes=30), "l": timedelta(minutes=10)},
                ),
                (
                    "SELECT count(*) FROM memory_claim_compaction(:g, :l, 1)",
                    {"g": timedelta(hours=1), "l": timedelta(minutes=10)},
                ),
                ("SELECT count(*) FROM kb_claim_index(:l, 1)", {"l": timedelta(minutes=10)}),
            ):
                assert (await conn.execute(text(sql), params)).scalar_one() in (0, 1)
            await tx.rollback()
    finally:
        await eng.dispose()


async def test_admin_login_switches_only_to_admin(login_roles: LoginRoles) -> None:
    eng = make_engine(login_url(login_roles.admin, login_roles.admin_password))
    try:
        assert await _can_set_role(eng, "feelings_admin")
        for denied in ("feelings_app", "feelings_worker", "feelings_api", "postgres"):
            assert not await _can_set_role(eng, denied), denied
        async with admin_scope(make_sessionmaker(eng)) as s:
            assert (await s.execute(text("SELECT current_user"))).scalar_one() == "feelings_admin"
    finally:
        await eng.dispose()


async def test_api_login_cannot_become_admin_or_worker(app_engine: AsyncEngine) -> None:
    """АР-10: роль входа API — только feelings_app; админ-API и воркер — свои роли входа."""
    assert await _can_set_role(app_engine, "feelings_app")
    for denied in ("feelings_admin", "feelings_worker"):
        assert not await _can_set_role(app_engine, denied), denied


async def test_rerun_rotates_password_and_keeps_memberships(
    engine: AsyncEngine, login_roles: LoginRoles
) -> None:
    async def verifier() -> object:
        async with engine.connect() as conn:
            return (
                await conn.execute(
                    text("SELECT rolpassword FROM pg_authid WHERE rolname = :n"),
                    {"n": login_roles.worker},
                )
            ).scalar_one()

    old = await verifier()
    rotated = new_password()
    done = run_roles_sql(
        env={"FEELINGS_WORKER_DB_PASSWORD": rotated},
        worker=login_roles.worker,
        admin=login_roles.admin,
    )
    assert done.returncode == 0, done.stderr
    assert await verifier() != old
    eng = make_engine(login_url(login_roles.worker, rotated))
    try:
        assert await _can_set_role(eng, "feelings_worker")
        assert await _can_set_role(eng, "feelings_app")
        assert not await _can_set_role(eng, "feelings_admin")
    finally:
        await eng.dispose()


async def test_script_refuses_login_role_with_extra_membership(engine: AsyncEngine) -> None:
    """Существующая роль входа, которая уже может стать feelings_admin, воркером не станет:
    скрипт останавливается, транзакция откатывается (членств не добавлено)."""
    name = f"t_wlogin_{uuid.uuid4().hex[:10]}"
    async with engine.connect() as conn:
        await conn.execute(text(f"CREATE ROLE {name} LOGIN NOINHERIT IN ROLE feelings_admin"))
        await conn.commit()
    try:
        done = run_roles_sql(
            env={"FEELINGS_WORKER_DB_PASSWORD": new_password()},
            worker=name,
            admin=f"t_alogin_{uuid.uuid4().hex[:10]}",
        )
        assert done.returncode != 0
        assert "feelings_admin" in done.stderr and "лишними ролями" in done.stderr
        async with engine.connect() as conn:
            got = await conn.execute(
                text(
                    "SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid "
                    "JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = :n ORDER BY 1"
                ),
                {"n": name},
            )
            assert got.scalars().all() == ["feelings_admin"]
    finally:
        await _drop_roles(engine, name)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "не задан ни один пароль"),
        ({"FEELINGS_WORKER_DB_PASSWORD": "short"}, "FEELINGS_WORKER_DB_PASSWORD"),
        ({"FEELINGS_ADMIN_DB_PASSWORD": "a" * 40 + "@"}, "FEELINGS_ADMIN_DB_PASSWORD"),
    ],
)
async def test_script_rejects_bad_input_without_changes(
    engine: AsyncEngine, env: dict[str, str], message: str
) -> None:
    tag = uuid.uuid4().hex[:10]
    worker, admin = f"t_wlogin_{tag}", f"t_alogin_{tag}"
    before = await _role_snapshot(engine)
    try:
        done = run_roles_sql(env=env, worker=worker, admin=admin)
        assert done.returncode != 0
        assert message in done.stderr
        for value in env.values():
            assert value not in done.stderr and value not in done.stdout
    finally:
        await _drop_roles(engine, worker, admin)
    assert await _role_snapshot(engine) == before


@pytest.mark.parametrize("name", ["feelings_api", "feelings_admin", "postgres", "Bad-Name"])
async def test_script_refuses_reserved_or_privileged_names(engine: AsyncEngine, name: str) -> None:
    before = await _role_snapshot(engine)
    done = run_roles_sql(
        env={"FEELINGS_WORKER_DB_PASSWORD": new_password()},
        worker=name,
        admin=f"t_alogin_{uuid.uuid4().hex[:10]}",
    )
    assert done.returncode != 0
    assert await _role_snapshot(engine) == before


async def test_dry_run_checks_and_rolls_back(engine: AsyncEngine) -> None:
    tag = uuid.uuid4().hex[:10]
    worker, admin = f"t_wlogin_{tag}", f"t_alogin_{tag}"
    try:
        done = run_roles_sql(
            env={k: new_password() for k in PASSWORD_ENVS}, worker=worker, admin=admin, dry_run=True
        )
        assert done.returncode == 0, done.stderr
        assert "dry_run" in done.stdout
        async with engine.connect() as conn:
            left = await conn.execute(
                text("SELECT count(*) FROM pg_roles WHERE rolname = ANY(:n)"),
                {"n": [worker, admin]},
            )
            assert left.scalar_one() == 0
    finally:
        await _drop_roles(engine, worker, admin)


async def test_api_password_path_dry_run_leaves_roles_unchanged(engine: AsyncEngine) -> None:
    """Ветка FEELINGS_API_DB_PASSWORD (ALTER ROLE feelings_api …) выполняется и проверяется, но
    в dry_run откатывается: снимок групповых ролей и хеш пароля feelings_api не меняются."""
    tag = uuid.uuid4().hex[:10]
    worker, admin = f"t_wlogin_{tag}", f"t_alogin_{tag}"
    before = await _role_snapshot(engine)
    password = new_password()
    try:
        done = run_roles_sql(
            env={"FEELINGS_API_DB_PASSWORD": password}, worker=worker, admin=admin, dry_run=True
        )
        assert done.returncode == 0, done.stderr
        assert "dry_run" in done.stdout
        assert password not in done.stdout and password not in done.stderr
    finally:
        await _drop_roles(engine, worker, admin)
    assert await _role_snapshot(engine) == before


@pytest.fixture
async def manager_url(engine: AsyncEngine) -> AsyncIterator[tuple[str, str]]:
    """Временная роль-управляющий без суперпользователя: CREATEROLE и ADMIN OPTION на группы
    (как у оператора управляемого PostgreSQL). Удаляется вместе со своими членствами."""
    name, password = f"t_mgr_{uuid.uuid4().hex[:10]}", new_password()
    async with engine.connect() as conn:
        await conn.execute(text(f"CREATE ROLE {name} LOGIN CREATEROLE PASSWORD '{password}'"))
        await conn.execute(
            text(f"GRANT feelings_worker, feelings_app, feelings_admin TO {name} WITH ADMIN OPTION")
        )
        await conn.commit()
    parts = urlsplit(_psql_url())
    host = parts.netloc.split("@")[-1]
    try:
        yield name, urlunsplit(parts._replace(netloc=f"{name}:{password}@{host}"))
    finally:
        await _drop_roles(engine, name)


async def test_non_superuser_runs_without_privileged_attributes(
    engine: AsyncEngine, manager_url: tuple[str, str]
) -> None:
    _, url = manager_url
    tag = uuid.uuid4().hex[:10]
    worker, admin = f"t_wlogin_{tag}", f"t_alogin_{tag}"
    try:
        done = run_roles_sql(
            env={k: new_password() for k in PASSWORD_ENVS},
            worker=worker,
            admin=admin,
            dry_run=True,
            url=url,
        )
        assert done.returncode == 0, done.stderr
        assert "dry_run" in done.stdout
    finally:
        await _drop_roles(engine, worker, admin)


async def test_non_superuser_stops_when_statements_are_logged(
    engine: AsyncEngine, manager_url: tuple[str, str]
) -> None:
    """Без суперпользователя журналирование не отключить: при log_statement = ddl скрипт
    останавливается до первого запроса с паролем; явное -v allow_logged=1 — продолжает."""
    name, url = manager_url
    async with engine.connect() as conn:
        await conn.execute(text(f"ALTER ROLE {name} SET log_statement = 'ddl'"))
        await conn.commit()
    tag = uuid.uuid4().hex[:10]
    worker, admin = f"t_wlogin_{tag}", f"t_alogin_{tag}"
    password = new_password()
    try:
        refused = run_roles_sql(
            env={"FEELINGS_WORKER_DB_PASSWORD": password}, worker=worker, admin=admin, url=url
        )
        assert refused.returncode != 0
        assert "журналируются" in refused.stderr
        allowed = run_roles_sql(
            env={"FEELINGS_WORKER_DB_PASSWORD": password},
            worker=worker,
            admin=admin,
            url=url,
            dry_run=True,
            allow_logged=True,
        )
        assert allowed.returncode == 0, allowed.stderr
        assert "allow_logged" in allowed.stdout
    finally:
        await _drop_roles(engine, worker, admin)

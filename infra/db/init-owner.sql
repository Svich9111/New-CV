-- Однократная инициализация БД «Чувства AI» суперпользователем (задача 0.8). Единственный шаг,
-- которому нужен пароль суперпользователя; дальше миграции идут под владельцем схемы
-- feelings_owner, приложение — под feelings_api, воркеры и админ-API — под своими ролями входа
-- (infra/db/roles.sql). Пароль суперпользователя хранится отдельно и для работы стенда не нужен.
--
-- Что делает (в одной транзакции, повторный запуск безопасен; п. 6 — загрузка vector в сеансах
-- владельца, см. ниже):
--   1. роль владельца схемы feelings_owner: LOGIN, CREATEROLE (групповые роли feelings_* и роли
--      входа из roles.sql), без SUPERUSER / CREATEDB / REPLICATION / BYPASSRLS. Пароль — из
--      FEELINGS_OWNER_DB_PASSWORD (как в roles.sql: \getenv);
--   2. журнал для всех сеансов владельца: текст запросов не пишется ни всегда, ни при ошибке
--      (log_statement = none, log_min_error_statement = panic, …) — иначе сбой `ALTER ROLE …
--      PASSWORD` в roles.sql (db-roles идёт под владельцем) вывел бы пароль в журнал
--      (ревью 0.8, MINOR-9). Эти настройки задаёт только суперпользователь — поэтому здесь;
--   3. групповые роли feelings_app / feelings_worker / feelings_admin / feelings_api (как в
--      миграции 0001) — от имени владельца, чтобы он получил ADMIN на них (db-roles). Роли
--      кластера в pg_dump не входят: без этого шага восстановление копии на НОВЫЙ сервер
--      (db-init → pg_restore → db-roles) падало бы на GRANT несуществующим ролям. Уже
--      существующие роли не пересоздаются; если у владельца нет ADMIN на них — выдаётся
--      (WITH ADMIN, без INHERIT и SET — так PG16 записывает создателя роли);
--   4. база — во владении feelings_owner; схема public в PG ≥ 15 принадлежит pg_database_owner,
--      т. е. тоже владельцу базы. Права уровня базы как в миграции 0001 (АР-10): у PUBLIC
--      отозваны TEMPORARY и CREATE, остаётся CONNECT. Права базы не входят в pg_dump без
--      --create — поэтому init выполняется и перед восстановлением копии (ревью 0.8, MAJOR-1);
--   5. расширение vector (не «доверенное» — создать его может только суперпользователь). Его
--      функции остаются во владении суперпользователя, поэтому `REVOKE ALL ON ALL FUNCTIONS IN
--      SCHEMA public FROM PUBLIC` из 0001 под владельцем их не задел бы (только WARNING) — и
--      приведение к vector снова стало бы доступно приложению (АР-35). Поэтому здесь: EXECUTE
--      функций расширения отзывается у PUBLIC и выдаётся только feelings_owner (им пользуются
--      функции SECURITY DEFINER, которыми он владеет). Итог совпадает с прогоном 0001 под
--      суперпользователем;
--   6. session_preload_libraries = vector для владельца в этой базе (см. ниже).
-- В конце — самопроверка (атрибуты владельца, права базы, EXECUTE функций vector).
--
-- Запуск (база уже создана, например POSTGRES_DB=feelings у образа postgres):
--   FEELINGS_OWNER_DB_PASSWORD=... psql "postgresql://postgres@HOST/feelings" -X \
--     -v ON_ERROR_STOP=1 -f infra/db/init-owner.sql
-- Пароль: ≥ 32 символов A-Z a-z 0-9 _ -. Не запускать с psql -a / -e / -E.
-- Необязательные переменные psql:
--   -v keep_owner_password=1 — пароль владельца не задаётся и не меняется (роль уже должна
--      существовать): так restore-check.sh готовит временную базу проверки копии;
--   -v owner_role=ИМЯ — другое имя владельца (только автотест backend/tests/test_owner_migrations.py:
--      роли кластера общие, тест работает со своей временной ролью). Стенд — всегда feelings_owner.

\set ON_ERROR_STOP on
\set QUIET on
\getenv owner_password FEELINGS_OWNER_DB_PASSWORD
\if :{?owner_role}
\else
\set owner_role feelings_owner
\endif
\if :{?keep_owner_password}
\else
\set keep_owner_password 0
\endif

-- Настройки журнала — на сеанс и до BEGIN (выборка транзакций решается в её начале).
-- Их меняет только суперпользователь: иначе скрипт останавливается здесь же.
SET log_statement = 'none';
SET log_min_duration_statement = -1;
SET log_min_duration_sample = -1;
SET log_transaction_sample_rate = 0;
SET log_min_error_statement = 'panic';
BEGIN;
SET LOCAL password_encryption = 'scram-sha-256';

SELECT set_config('feelings_init.owner', :'owner_role', true) AS ignored,
       set_config('feelings_init.keep_pw', CASE WHEN :'keep_owner_password'::boolean THEN '1' ELSE '' END, true) AS ignored2
\gset

DO $chk$
DECLARE
    v_owner text := current_setting('feelings_init.owner');
BEGIN
    IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
        RAISE EXCEPTION 'init-owner.sql запускается суперпользователем';
    END IF;
    IF v_owner !~ '^[a-z_][a-z0-9_]{0,62}$' OR v_owner LIKE 'pg\_%'
       OR v_owner IN ('feelings_app', 'feelings_worker', 'feelings_admin', 'feelings_api') THEN
        RAISE EXCEPTION 'имя владельца «%»: только a-z 0-9 _, не pg_*, не групповая роль', v_owner;
    END IF;
END $chk$;

\if :keep_owner_password
\else
\if :{?owner_password}
SELECT set_config('feelings_init.pw_ok', (:'owner_password' ~ '^[A-Za-z0-9_-]{32,}$')::text, true) AS ignored \gset
\endif
\endif
DO $pw$
DECLARE
    v_owner text := current_setting('feelings_init.owner');
BEGIN
    IF current_setting('feelings_init.keep_pw') <> '' THEN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_owner) THEN
            RAISE EXCEPTION 'keep_owner_password=1: роли «%» нет — сначала обычный db-init с паролем', v_owner;
        END IF;
    ELSIF coalesce(current_setting('feelings_init.pw_ok', true), '') <> 'true' THEN
        RAISE EXCEPTION 'FEELINGS_OWNER_DB_PASSWORD: нужно не меньше 32 символов A-Z a-z 0-9 _ -';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_owner
               AND (rolsuper OR rolbypassrls OR rolreplication)) THEN
        RAISE EXCEPTION 'роль «%» уже есть и имеет особые права — разберитесь вручную', v_owner;
    END IF;
END $pw$;

-- 1. Владелец.
SELECT format('CREATE ROLE %I', :'owner_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'owner_role') \gexec
SELECT format('ALTER ROLE %I LOGIN INHERIT CREATEROLE NOSUPERUSER NOCREATEDB NOREPLICATION NOBYPASSRLS',
              :'owner_role') \gexec
\if :keep_owner_password
\else
SELECT format('ALTER ROLE %I PASSWORD %L', :'owner_role', :'owner_password') \gexec
\endif

-- 2. Журнал сеансов владельца (на роль, во всех базах).
SELECT format('ALTER ROLE %I SET %s', :'owner_role', s)
FROM unnest(ARRAY['log_statement = none', 'log_min_error_statement = panic',
                  'log_min_duration_statement = -1', 'log_min_duration_sample = -1',
                  'log_transaction_sample_rate = 0']) AS s \gexec

-- 3. Групповые роли (как в 0001) — от имени владельца (он становится их создателем с ADMIN).
-- (по oid существующих ролей: имя несуществующей роли pg_has_role не принимает)
SELECT format('GRANT %I TO %I WITH ADMIN TRUE, INHERIT FALSE, SET FALSE', r.rolname, :'owner_role')
FROM pg_roles r
WHERE r.rolname IN ('feelings_app', 'feelings_worker', 'feelings_admin', 'feelings_api')
  AND NOT pg_has_role(:'owner_role', r.oid, 'MEMBER WITH ADMIN OPTION') \gexec
SELECT format('SET LOCAL ROLE %I', :'owner_role') \gexec
SELECT 'CREATE ROLE feelings_app NOLOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'feelings_app') \gexec
SELECT 'CREATE ROLE feelings_worker NOLOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'feelings_worker') \gexec
SELECT 'CREATE ROLE feelings_admin NOLOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'feelings_admin') \gexec
SELECT 'CREATE ROLE feelings_api NOLOGIN NOINHERIT' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'feelings_api') \gexec
-- Членство feelings_api в feelings_app (0001) — только если его нет ни от кого.
SELECT 'GRANT feelings_app TO feelings_api'
WHERE NOT EXISTS (SELECT 1 FROM pg_auth_members
                  WHERE roleid = 'feelings_app'::regrole AND member = 'feelings_api'::regrole) \gexec
RESET ROLE;

-- 4. База и схема; права уровня базы (0001, АР-10).
SELECT format('ALTER DATABASE %I OWNER TO %I', current_database(), :'owner_role') \gexec
-- Схема public (PG ≥ 15 — владелец pg_database_owner); если её владелец другой — передать.
SELECT format('ALTER SCHEMA public OWNER TO %I', :'owner_role')
WHERE (SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'public')
      NOT IN ('pg_database_owner', :'owner_role') \gexec
-- REVOKE — от имени владельца базы (он и выдавал права PUBLIC по умолчанию).
SELECT format('SET LOCAL ROLE %I', :'owner_role') \gexec
SELECT format('REVOKE TEMPORARY, CREATE ON DATABASE %I FROM PUBLIC', current_database()) \gexec
RESET ROLE;

-- 5. Расширение vector: EXECUTE — только владельцу.
CREATE EXTENSION IF NOT EXISTS vector;
SELECT format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', p.oid::regprocedure),
       format('GRANT EXECUTE ON FUNCTION %s TO %I', p.oid::regprocedure, :'owner_role')
FROM pg_proc p
JOIN pg_depend d ON d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e'
JOIN pg_extension e ON e.oid = d.refobjid AND e.extname = 'vector' \gexec

-- 6. Библиотека vector загружается в каждом сеансе владельца. Иначе параметр hnsw.ef_search
-- (SET в функциях 0008/0010) до первой загрузки библиотеки — «заглушка», и не-суперпользователь
-- не может задать её в CREATE FUNCTION … SET: падает `alembic downgrade` (откат), где функции
-- пересоздаются раньше любого обращения к vector. session_preload_libraries меняет только
-- суперпользователь — поэтому здесь.
SELECT format('ALTER ROLE %I IN DATABASE %I SET session_preload_libraries = %L',
              :'owner_role', current_database(), 'vector') \gexec

-- Проверка: владелец без особых прав; права базы; PUBLIC не исполняет функции vector.
DO $verify$
DECLARE
    v_owner text := current_setting('feelings_init.owner');
    v_db text := current_database();
    v_role text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_owner
               AND (rolsuper OR rolbypassrls OR rolreplication OR rolcreatedb OR NOT rolcanlogin)) THEN
        RAISE EXCEPTION '%: неверные атрибуты', v_owner;
    END IF;
    IF (SELECT datdba::regrole::text FROM pg_database WHERE datname = v_db) <> v_owner THEN
        RAISE EXCEPTION 'база % не во владении %', v_db, v_owner;
    END IF;
    FOREACH v_role IN ARRAY ARRAY['public', 'feelings_app', 'feelings_api', 'feelings_worker', 'feelings_admin'] LOOP
        IF has_database_privilege(v_role, v_db, 'TEMPORARY') OR has_database_privilege(v_role, v_db, 'CREATE') THEN
            RAISE EXCEPTION 'роль % может создавать временные таблицы или схемы в базе %', v_role, v_db;
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_proc p
               JOIN pg_depend d ON d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e'
               JOIN pg_extension e ON e.oid = d.refobjid AND e.extname = 'vector'
               WHERE has_function_privilege('public', p.oid, 'EXECUTE')) THEN
        RAISE EXCEPTION 'функции vector исполняемы для PUBLIC';
    END IF;
    IF NOT pg_has_role(v_owner, 'feelings_app', 'MEMBER WITH ADMIN OPTION')
       OR NOT pg_has_role(v_owner, 'feelings_api', 'MEMBER WITH ADMIN OPTION')
       OR NOT pg_has_role('feelings_api', 'feelings_app', 'SET') THEN
        RAISE EXCEPTION 'групповые роли feelings_*: у % нет ADMIN или feelings_api не член feelings_app', v_owner;
    END IF;
END $verify$;

COMMIT;
\echo 'init-owner.sql: владелец схемы, групповые роли, права базы и расширение vector готовы.'

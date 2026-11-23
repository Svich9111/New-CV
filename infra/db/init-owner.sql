-- Однократная инициализация БД «Чувства AI» суперпользователем (задача 0.8). Единственный шаг,
-- которому нужен пароль суперпользователя; дальше миграции идут под владельцем схемы
-- feelings_owner, приложение — под feelings_api, воркеры и админ-API — под своими ролями входа
-- (infra/db/roles.sql). Пароль суперпользователя хранится отдельно и для работы стенда не нужен.
--
-- Что делает (в одной транзакции, повторный запуск безопасен; п. 4 — загрузка vector в сеансах
-- владельца, см. ниже):
--   1. роль владельца схемы feelings_owner: LOGIN, CREATEROLE (групповые роли feelings_* из
--      миграции 0001 и роли входа из roles.sql), без SUPERUSER / CREATEDB / REPLICATION /
--      BYPASSRLS. Пароль — из FEELINGS_OWNER_DB_PASSWORD (как в roles.sql: \getenv);
--   2. база — во владении feelings_owner (REVOKE TEMPORARY ON DATABASE в 0001); схема public
--      в PG ≥ 15 принадлежит pg_database_owner, т. е. тоже владельцу базы;
--   3. расширение vector (не «доверенное» — создать его может только суперпользователь). Его
--      функции остаются во владении суперпользователя, поэтому `REVOKE ALL ON ALL FUNCTIONS IN
--      SCHEMA public FROM PUBLIC` из 0001 под владельцем их не задел бы (только WARNING) — и
--      приведение к vector снова стало бы доступно приложению (АР-35). Поэтому здесь: EXECUTE
--      функций расширения отзывается у PUBLIC и выдаётся только feelings_owner (им пользуются
--      функции SECURITY DEFINER, которыми он владеет). Итог совпадает с прогоном 0001 под
--      суперпользователем.
--
-- Запуск (база уже создана, например POSTGRES_DB=feelings у образа postgres):
--   FEELINGS_OWNER_DB_PASSWORD=... psql "postgresql://postgres@HOST/feelings" -X \
--     -v ON_ERROR_STOP=1 -f infra/db/init-owner.sql
-- Пароль: ≥ 32 символов A-Z a-z 0-9 _ -. Не запускать с psql -a / -e / -E.

\set ON_ERROR_STOP on
\set QUIET on
\getenv owner_password FEELINGS_OWNER_DB_PASSWORD

-- Настройки журнала — на сеанс и до BEGIN (выборка транзакций решается в её начале).
-- Их меняет только суперпользователь: иначе скрипт останавливается здесь же.
SET log_statement = 'none';
SET log_min_duration_statement = -1;
SET log_min_duration_sample = -1;
SET log_transaction_sample_rate = 0;
SET log_min_error_statement = 'panic';
BEGIN;
SET LOCAL password_encryption = 'scram-sha-256';

DO $chk$ BEGIN
    IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
        RAISE EXCEPTION 'init-owner.sql запускается суперпользователем';
    END IF;
END $chk$;

\if :{?owner_password}
SELECT set_config('feelings_init.pw_ok', (:'owner_password' ~ '^[A-Za-z0-9_-]{32,}$')::text, true) AS ignored \gset
\endif
DO $pw$ BEGIN
    IF coalesce(current_setting('feelings_init.pw_ok', true), '') <> 'true' THEN
        RAISE EXCEPTION 'FEELINGS_OWNER_DB_PASSWORD: нужно не меньше 32 символов A-Z a-z 0-9 _ -';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'feelings_owner'
               AND (rolsuper OR rolbypassrls OR rolreplication)) THEN
        RAISE EXCEPTION 'роль feelings_owner уже есть и имеет особые права — разберитесь вручную';
    END IF;
END $pw$;

SELECT 'CREATE ROLE feelings_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'feelings_owner') \gexec
SELECT format('ALTER ROLE feelings_owner LOGIN INHERIT CREATEROLE NOSUPERUSER NOCREATEDB NOREPLICATION NOBYPASSRLS PASSWORD %L',
              :'owner_password') \gexec

SELECT format('ALTER DATABASE %I OWNER TO feelings_owner', current_database()) \gexec
-- Схема public (PG ≥ 15 — владелец pg_database_owner); если её владелец другой — передать.
SELECT 'ALTER SCHEMA public OWNER TO feelings_owner'
WHERE (SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'public')
      NOT IN ('pg_database_owner', 'feelings_owner') \gexec

CREATE EXTENSION IF NOT EXISTS vector;
SELECT format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', p.oid::regprocedure),
       format('GRANT EXECUTE ON FUNCTION %s TO feelings_owner', p.oid::regprocedure)
FROM pg_proc p
JOIN pg_depend d ON d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e'
JOIN pg_extension e ON e.oid = d.refobjid AND e.extname = 'vector' \gexec

-- Библиотека vector загружается в каждом сеансе владельца. Иначе параметр hnsw.ef_search
-- (SET в функциях 0008/0010) до первой загрузки библиотеки — «заглушка», и не-суперпользователь
-- не может задать её в CREATE FUNCTION … SET: падает `alembic downgrade` (откат), где функции
-- пересоздаются раньше любого обращения к vector. session_preload_libraries меняет только
-- суперпользователь — поэтому здесь.
SELECT format('ALTER ROLE feelings_owner IN DATABASE %I SET session_preload_libraries = %L',
              current_database(), 'vector') \gexec

-- Проверка: владелец без особых прав; PUBLIC не исполняет функции vector.
DO $verify$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'feelings_owner'
               AND (rolsuper OR rolbypassrls OR rolreplication OR rolcreatedb OR NOT rolcanlogin)) THEN
        RAISE EXCEPTION 'feelings_owner: неверные атрибуты';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_proc p
               JOIN pg_depend d ON d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e'
               JOIN pg_extension e ON e.oid = d.refobjid AND e.extname = 'vector'
               WHERE has_function_privilege('public', p.oid, 'EXECUTE')) THEN
        RAISE EXCEPTION 'функции vector исполняемы для PUBLIC';
    END IF;
END $verify$;

COMMIT;
\echo 'init-owner.sql: владелец схемы feelings_owner и расширение vector готовы.'

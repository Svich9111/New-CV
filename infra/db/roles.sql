-- Роли входа «Чувства AI» (Б-69, Б-75; АР-10). Запускает оператор/инфраструктура ПОСЛЕ
-- `alembic upgrade head`: групповые роли feelings_app / feelings_worker / feelings_admin /
-- feelings_api создаёт миграция 0001 (NOLOGIN), а роли входа с паролями — этот скрипт.
--
-- Почему не миграция: пароль — секрет окружения (CLAUDE.md, ТЗ §5), а миграции лежат в
-- репозитории, выполняются на всех стендах одинаково и попадают в историю Alembic; роли и их
-- пароли общие для всего кластера PostgreSQL (не для одной БД), различаются по стендам и
-- ротируются без смены схемы. Поэтому схема создаёт только групповые роли без входа и права,
-- а вход и пароли задаёт инфраструктура (АР-10, 0001: «LOGIN и пароль задаёт инфраструктура»).
--
-- Что делает (в одной транзакции; любая ошибка — откат всего, ничего не меняется):
--   * роль входа воркера (по умолчанию feelings_worker_login): LOGIN NOINHERIT, член
--     feelings_worker и feelings_app (INHERIT FALSE, SET TRUE) — воркеры памяти и базы знаний
--     (app.workers.memory, app.workers.kb) делают SET LOCAL ROLE feelings_worker (захват работы)
--     и feelings_app (обработка от имени владельца, user_scope). FEELINGS_WORKER_DATABASE_URL.
--   * роль входа администратора (по умолчанию feelings_admin_login): LOGIN NOINHERIT, член
--     только feelings_admin — админ-API базы знаний (admin_scope). FEELINGS_ADMIN_DATABASE_URL.
--   * роль входа API feelings_api (создана миграцией): LOGIN и пароль. FEELINGS_DATABASE_URL.
--   * проверка итога: каждая роль входа может стать ТОЛЬКО своими ролями (с учётом вложенных
--     членств), сама прав не наследует, не суперпользователь; feelings_api не может стать
--     feelings_admin / feelings_worker (АР-10).
--
-- Пароли — только из переменных окружения процесса psql (не в файле, не в командной строке):
--   FEELINGS_WORKER_DB_PASSWORD, FEELINGS_ADMIN_DB_PASSWORD, FEELINGS_API_DB_PASSWORD.
-- Не задана переменная — соответствующая роль не создаётся и не меняется (нужна хотя бы одна).
-- Пароль: не короче 32 символов, только A-Z a-z 0-9 _ - (подставляется в URL без кодирования):
--   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
--
-- Запуск (под владельцем кластера или ролью с CREATEROLE и ADMIN OPTION на группы):
--   FEELINGS_WORKER_DB_PASSWORD=... FEELINGS_ADMIN_DB_PASSWORD=... FEELINGS_API_DB_PASSWORD=... \
--     psql "postgresql://postgres@HOST:5432/feelings" -v ON_ERROR_STOP=1 -f infra/db/roles.sql
-- Необязательные переменные psql: -v worker_login=ИМЯ -v admin_login=ИМЯ (имена ролей входа),
-- -v dry_run=1 (всё выполнить и проверить, затем ROLLBACK), -v allow_logged=1 (см. ниже про
-- журнал). Не запускать с psql -a / -e / -E: они печатают запросы вместе с паролем. Повторный запуск безопасен: роли
-- обновляются (новый пароль = ротация), лишние членства не снимаются, а останавливают скрипт.
-- Нужен psql ≥ 15 (\getenv). Подробно — docs/ADMIN.md, раздел «Роли БД».

\set ON_ERROR_STOP on
\set QUIET on

\getenv worker_password FEELINGS_WORKER_DB_PASSWORD
\getenv admin_password FEELINGS_ADMIN_DB_PASSWORD
\getenv api_password FEELINGS_API_DB_PASSWORD

\if :{?worker_login}
\else
\set worker_login feelings_worker_login
\endif
\if :{?admin_login}
\else
\set admin_login feelings_admin_login
\endif
\if :{?dry_run}
\else
\set dry_run 0
\endif


-- Пароли не должны попасть в журнал сервера и статистику запросов: ни сам запрос
-- (log_statement, log_min_duration_statement), ни текст запроса при ошибке
-- (log_min_error_statement), ни текст служебной команды в pg_stat_statements (track_utility).
-- Под суперпользователем скрипт отключает это сам. Без суперпользователя менять эти настройки
-- нельзя: если запросы журналируются (log_statement = ddl/mod/all, log_min_duration_statement ≠ -1,
-- выборочно: log_min_duration_sample ≠ -1, log_transaction_sample_rate > 0)
-- или pg_stat_statements записывает служебные команды, скрипт останавливается — разве что
-- передано явное согласие -v allow_logged=1 (тогда пароли сменить сразу после, docs/ADMIN.md).
\if :{?allow_logged}
\else
\set allow_logged 0
\endif
SELECT rolsuper AS is_super,
       current_setting('pg_stat_statements.track_utility', true) IS NOT NULL AS has_pgss
FROM pg_roles WHERE rolname = current_user \gset
-- SUPERUSER / CREATEDB / REPLICATION / BYPASSRLS меняет только суперпользователь. Без него эти пункты
-- опускаются: новая роль их не получит, а существующую с ними скрипт не трогает (проверка ниже).
\if :is_super
\set super_attrs ' NOSUPERUSER NOCREATEDB NOREPLICATION NOBYPASSRLS'
\else
\set super_attrs ''
\endif
\if :is_super
SET log_statement = 'none';
SET log_min_duration_statement = -1;
SET log_min_duration_sample = -1;
SET log_transaction_sample_rate = 0;
SET log_min_error_statement = 'panic';
\if :has_pgss
SET pg_stat_statements.track_utility = off;
\endif
\else
SELECT current_setting('log_statement') IN ('ddl', 'mod', 'all')
       OR current_setting('log_min_duration_statement') <> '-1'
       OR current_setting('log_min_duration_sample') <> '-1'
       OR current_setting('log_transaction_sample_rate')::float8 > 0
       OR coalesce(current_setting('pg_stat_statements.track_utility', true), 'off') = 'on'
       AS logged \gset
\if :logged
\if :allow_logged
\echo 'ВНИМАНИЕ: запросы журналируются (allow_logged=1) — пароли могут остаться в журнале; смените их повторно после отключения журнала.'
\else
DO $log$ BEGIN
    RAISE EXCEPTION 'запросы журналируются (log_statement / log_min_duration_statement / pg_stat_statements.track_utility) — пароль попадёт в журнал; запустите под суперпользователем, отключите журналирование или передайте -v allow_logged=1';
END $log$;
\endif
\else
\echo 'ВНИМАНИЕ: не суперпользователь — при ошибке запроса текст с паролем может попасть в журнал (log_min_error_statement).'
\endif
\endif
-- Транзакция — после настроек журнала: выборка log_transaction_sample_rate решается в начале
-- транзакции, поэтому отключать её нужно до BEGIN (на весь сеанс psql).
BEGIN;
SET LOCAL password_encryption = 'scram-sha-256';

-- Пустая переменная окружения = не задана (запросы с паролями — только после отключения журнала).
\if :{?worker_password}
SELECT :'worker_password' <> '' AS do_worker \gset
\else
\set do_worker 0
\endif
\if :{?admin_password}
SELECT :'admin_password' <> '' AS do_admin \gset
\else
\set do_admin 0
\endif
\if :{?api_password}
SELECT :'api_password' <> '' AS do_api \gset
\else
\set do_api 0
\endif


-- Имена и признаки — в настройки транзакции, чтобы их видели проверки в DO-блоках.
SELECT set_config('feelings_roles.worker_login', :'worker_login', true),
       set_config('feelings_roles.admin_login', :'admin_login', true),
       set_config('feelings_roles.do_worker', CASE WHEN :'do_worker'::boolean THEN '1' ELSE '' END, true),
       set_config('feelings_roles.do_admin', CASE WHEN :'do_admin'::boolean THEN '1' ELSE '' END, true),
       set_config('feelings_roles.do_api', CASE WHEN :'do_api'::boolean THEN '1' ELSE '' END, true),
       set_config('feelings_roles.is_super', CASE WHEN :'is_super'::boolean THEN '1' ELSE '' END, true)
\gset

-- Пароль: не короче 32 символов A-Z a-z 0-9 _ - (результат проверки — без самого пароля).
\if :do_worker
SELECT set_config('feelings_roles.worker_pw_ok', (:'worker_password' ~ '^[A-Za-z0-9_-]{32,}$')::text, true) AS ignored \gset
\endif
\if :do_admin
SELECT set_config('feelings_roles.admin_pw_ok', (:'admin_password' ~ '^[A-Za-z0-9_-]{32,}$')::text, true) AS ignored \gset
\endif
\if :do_api
SELECT set_config('feelings_roles.api_pw_ok', (:'api_password' ~ '^[A-Za-z0-9_-]{32,}$')::text, true) AS ignored \gset
\endif

-- Входные данные. Пароли в текст ошибок не попадают.
DO $chk$
DECLARE
    v_worker text := current_setting('feelings_roles.worker_login');
    v_admin  text := current_setting('feelings_roles.admin_login');
    v_name   text;
    v_reserved text[] := ARRAY['feelings_app', 'feelings_worker', 'feelings_admin', 'feelings_api'];
BEGIN
    IF current_setting('feelings_roles.do_worker') = '' AND current_setting('feelings_roles.do_admin') = ''
       AND current_setting('feelings_roles.do_api') = '' THEN
        RAISE EXCEPTION 'не задан ни один пароль (FEELINGS_WORKER_DB_PASSWORD, FEELINGS_ADMIN_DB_PASSWORD, FEELINGS_API_DB_PASSWORD)';
    END IF;
    FOREACH v_name IN ARRAY ARRAY['worker', 'admin', 'api'] LOOP
        IF current_setting('feelings_roles.do_' || v_name) <> ''
           AND coalesce(current_setting('feelings_roles.' || v_name || '_pw_ok', true), '') <> 'true' THEN
            RAISE EXCEPTION 'FEELINGS_%_DB_PASSWORD: нужно не меньше 32 символов A-Z a-z 0-9 _ -', upper(v_name);
        END IF;
    END LOOP;
    IF v_worker = v_admin THEN
        RAISE EXCEPTION 'роли входа воркера и администратора должны различаться';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'feelings_api')
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'feelings_worker')
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'feelings_admin')
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'feelings_app') THEN
        RAISE EXCEPTION 'групповых ролей feelings_* нет — сначала alembic upgrade head';
    END IF;
    FOREACH v_name IN ARRAY ARRAY[v_worker, v_admin] LOOP
        IF v_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'имя роли входа «%»: только a-z 0-9 _, не длиннее 63', v_name;
        END IF;
        IF v_name = ANY (v_reserved) OR v_name LIKE 'pg\_%' THEN
            RAISE EXCEPTION 'имя роли входа «%» зарезервировано', v_name;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_name
                   AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls)) THEN
            RAISE EXCEPTION 'роль «%» уже существует и имеет особые права — скрипт её не трогает', v_name;
        END IF;
    END LOOP;
    -- Без суперпользователя: права проверяются ДО запросов с паролем, чтобы предсказуемая ошибка
    -- ALTER ROLE / GRANT не вывела текст запроса с паролем в журнал (log_min_error_statement).
    IF current_setting('feelings_roles.is_super') = '' THEN
        IF NOT (SELECT rolcreaterole FROM pg_roles WHERE rolname = current_user) THEN
            RAISE EXCEPTION 'нужен суперпользователь или роль с CREATEROLE';
        END IF;
        FOR v_name IN
            SELECT r FROM (VALUES
                (v_worker, current_setting('feelings_roles.do_worker')),
                ('feelings_worker', current_setting('feelings_roles.do_worker')),
                ('feelings_app', current_setting('feelings_roles.do_worker')),
                (v_admin, current_setting('feelings_roles.do_admin')),
                ('feelings_admin', current_setting('feelings_roles.do_admin')),
                ('feelings_api', current_setting('feelings_roles.do_api'))) AS t(r, needed)
            WHERE needed <> '' AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r)
        LOOP
            IF NOT pg_has_role(current_user, v_name, 'MEMBER WITH ADMIN OPTION') THEN
                RAISE EXCEPTION 'у «%» нет ADMIN OPTION на роль «%» — запустите под суперпользователем или владельцем схемы', current_user, v_name;
            END IF;
        END LOOP;
    END IF;
END $chk$;

\if :do_worker
SELECT format('CREATE ROLE %I', :'worker_login')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'worker_login') \gexec
SELECT format('ALTER ROLE %I LOGIN NOINHERIT NOCREATEROLE%s PASSWORD %L',
              :'worker_login', :'super_attrs', :'worker_password') \gexec
SELECT format('GRANT feelings_worker TO %I WITH INHERIT FALSE, SET TRUE', :'worker_login') \gexec
SELECT format('GRANT feelings_app TO %I WITH INHERIT FALSE, SET TRUE', :'worker_login') \gexec
\endif

\if :do_admin
SELECT format('CREATE ROLE %I', :'admin_login')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'admin_login') \gexec
SELECT format('ALTER ROLE %I LOGIN NOINHERIT NOCREATEROLE%s PASSWORD %L',
              :'admin_login', :'super_attrs', :'admin_password') \gexec
SELECT format('GRANT feelings_admin TO %I WITH INHERIT FALSE, SET TRUE', :'admin_login') \gexec
\endif

\if :do_api
SELECT format('ALTER ROLE feelings_api LOGIN NOINHERIT NOCREATEROLE%s PASSWORD %L',
              :'super_attrs', :'api_password') \gexec
\endif

-- Проверка итога: роль входа может стать (SET ROLE, в т.ч. через вложенные членства) только
-- разрешёнными ролями и сама не получает их прав (NOINHERIT). Проверяются и роли, которые этот
-- запуск не менял, если они существуют, — чтобы чужое лишнее членство не осталось незамеченным.
DO $verify$
DECLARE
    v_login text;
    v_allowed text[];
    v_extra text;
    v_usage text;
    v_role pg_roles%ROWTYPE;
BEGIN
    FOR v_login, v_allowed IN
        SELECT * FROM (VALUES
            (current_setting('feelings_roles.worker_login'), ARRAY['feelings_worker', 'feelings_app']),
            (current_setting('feelings_roles.admin_login'), ARRAY['feelings_admin']),
            ('feelings_api', ARRAY['feelings_app'])) AS t(login, allowed)
    LOOP
        SELECT * INTO v_role FROM pg_roles WHERE rolname = v_login;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;
        IF v_role.rolsuper OR v_role.rolcreaterole OR v_role.rolcreatedb OR v_role.rolreplication
           OR v_role.rolbypassrls OR v_role.rolinherit THEN
            RAISE EXCEPTION 'роль «%»: ожидается NOINHERIT без особых прав', v_login;
        END IF;
        SELECT string_agg(r.rolname, ', ' ORDER BY r.rolname) INTO v_extra
        FROM pg_roles r
        WHERE r.rolname <> v_login AND r.rolname <> ALL (v_allowed)
          AND pg_has_role(v_login, r.oid, 'MEMBER');
        IF v_extra IS NOT NULL THEN
            RAISE EXCEPTION 'роль «%» может стать лишними ролями: % (разрешено: %)', v_login, v_extra,
                array_to_string(v_allowed, ', ');
        END IF;
        SELECT string_agg(r.rolname, ', ' ORDER BY r.rolname) INTO v_usage
        FROM pg_roles r
        WHERE r.rolname <> v_login AND pg_has_role(v_login, r.oid, 'USAGE');
        IF v_usage IS NOT NULL THEN
            RAISE EXCEPTION 'роль «%» наследует права: % (нужен INHERIT FALSE)', v_login, v_usage;
        END IF;
        -- Члены самой роли входа получили бы её доступ (SET ROLE через неё). Допустимо только
        -- членство без SET и INHERIT — так PG16 записывает создателя роли (ADMIN OPTION).
        SELECT string_agg(m.rolname, ', ' ORDER BY m.rolname) INTO v_extra
        FROM pg_auth_members am JOIN pg_roles m ON m.oid = am.member
        WHERE am.roleid = v_role.oid AND (am.set_option OR am.inherit_option);
        IF v_extra IS NOT NULL THEN
            RAISE EXCEPTION 'у роли «%» есть члены: % — скрипт такую роль не принимает', v_login, v_extra;
        END IF;
        -- Собственные права или объекты (GRANT … TO роль, владение): NOINHERIT от них не защищает.
        IF EXISTS (SELECT 1 FROM pg_shdepend
                   WHERE refclassid = 'pg_authid'::regclass AND refobjid = v_role.oid) THEN
            RAISE EXCEPTION 'у роли «%» есть собственные права или объекты — скрипт такую роль не принимает', v_login;
        END IF;
        IF EXISTS (SELECT 1 FROM unnest(v_allowed) a WHERE NOT pg_has_role(v_login, a, 'SET')) THEN
            RAISE EXCEPTION 'роль «%» не может SET ROLE в одну из: %', v_login, array_to_string(v_allowed, ', ');
        END IF;
    END LOOP;
END $verify$;

\if :dry_run
ROLLBACK;
\echo 'roles.sql: проверка пройдена, изменения отменены (dry_run).'
\else
COMMIT;
\echo 'roles.sql: роли входа готовы.'
\endif

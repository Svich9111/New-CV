#!/usr/bin/env bash
# Проверка восстановления из копии (ТЗ §10: раз в квартал; задача 0.8). Копия разворачивается
# во ВРЕМЕННУЮ базу того же сервера PostgreSQL ТЕМ ЖЕ порядком, что и настоящее восстановление
# (docs/DEPLOY.md §10): пустая база → infra/db/init-owner.sql (владелец, права уровня базы,
# vector) → pg_restore. Затем проверки, временная база удаляется; рабочая база feelings не
# затрагивается.
#   infra/deploy/restore-check.sh [файл.dump]   — по умолчанию самая свежая копия
#   infra/deploy/restore-check.sh --db feelings — только проверки уже восстановленной базы
#                                                 (после восстановления по DEPLOY §10), без
#                                                 восстановления и удаления
# Проверки:
#   - pg_restore без ошибок; версия схемы (alembic_version) есть; таблицы на месте, число строк
#     читается; расширение vector и политики RLS восстановлены;
#   - права уровня базы (АР-10, миграция 0001; ревью 0.8 MAJOR-1): ни PUBLIC, ни feelings_app /
#     feelings_api / feelings_worker / feelings_admin не могут создавать временные таблицы и
#     схемы — pg_dump без --create прав базы не переносит, их возвращает init-owner.sql;
#   - функции vector не исполняет PUBLIC и роли приложения (АР-35); объекты приложения — во
#     владении feelings_owner; feelings_api может стать только feelings_app.
# Печатает только имена таблиц и числа строк — без содержимого.
# Переменные: FEELINGS_BACKUP_DIR, FEELINGS_DEPLOY_ENV_FILE (как в backup.sh).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INIT_OWNER_SQL="${HERE}/../db/init-owner.sql"
BACKUP_DIR="${FEELINGS_BACKUP_DIR:-/opt/feelings/backups}"
COMPOSE=(docker compose -f "${HERE}/docker-compose.prod.yml")
if [[ -n "${FEELINGS_DEPLOY_ENV_FILE:-}" ]]; then
    COMPOSE+=(--env-file "${FEELINGS_DEPLOY_ENV_FILE}")
fi
psql_pg() { "${COMPOSE[@]}" exec -T postgres psql -X -q -v ON_ERROR_STOP=1 -U postgres "$@"; }

check_db() {
    local db="$1"
    psql_pg -d "${db}" -At <<'SQL'
DO $$
DECLARE
    v_role text;
    v_db text := current_database();
    v_bad text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM alembic_version) THEN
        RAISE EXCEPTION 'нет версии схемы (alembic_version)';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        RAISE EXCEPTION 'нет расширения vector';
    END IF;
    IF (SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace
        AND relkind = 'r' AND relrowsecurity) = 0 THEN
        RAISE EXCEPTION 'не восстановлены политики RLS';
    END IF;
    IF (SELECT datdba::regrole::text FROM pg_database WHERE datname = v_db) <> 'feelings_owner' THEN
        RAISE EXCEPTION 'база % не во владении feelings_owner', v_db;
    END IF;
    FOREACH v_role IN ARRAY ARRAY['public', 'feelings_app', 'feelings_api', 'feelings_worker', 'feelings_admin'] LOOP
        IF has_database_privilege(v_role, v_db, 'TEMPORARY') OR has_database_privilege(v_role, v_db, 'CREATE') THEN
            RAISE EXCEPTION 'права базы ослаблены: % может создавать временные таблицы или схемы (АР-10) — выполните db-init', v_role;
        END IF;
        IF v_role <> 'public' AND has_schema_privilege(v_role, 'public', 'CREATE') THEN
            RAISE EXCEPTION 'права схемы ослаблены: % может создавать объекты в public (АР-10)', v_role;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_proc p
                   JOIN pg_depend d ON d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e'
                   JOIN pg_extension e ON e.oid = d.refobjid AND e.extname = 'vector'
                   WHERE has_function_privilege(v_role, p.oid, 'EXECUTE')) THEN
            RAISE EXCEPTION 'функции vector исполняемы для % (АР-35) — выполните db-init', v_role;
        END IF;
    END LOOP;
    SELECT string_agg(x, ', ') INTO v_bad FROM (
        SELECT c.relname::text AS x FROM pg_class c
        WHERE c.relnamespace = 'public'::regnamespace AND c.relowner::regrole::text <> 'feelings_owner'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e')
        UNION ALL
        SELECT p.oid::regprocedure::text FROM pg_proc p
        WHERE p.pronamespace = 'public'::regnamespace AND p.proowner::regrole::text <> 'feelings_owner'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')) s;
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'объекты не во владении feelings_owner: %', v_bad;
    END IF;
    SELECT string_agg(rolname, ', ') INTO v_bad FROM pg_roles
    WHERE rolname <> 'feelings_api' AND pg_has_role('feelings_api', oid, 'MEMBER')
      AND rolname <> 'feelings_app';
    IF v_bad IS NOT NULL OR NOT pg_has_role('feelings_api', 'feelings_app', 'SET') THEN
        RAISE EXCEPTION 'feelings_api: ожидается членство только в feelings_app (лишние: %)', coalesce(v_bad, '—');
    END IF;
END $$;
SELECT 'схема: ' || version_num FROM alembic_version;
SELECT format('%s: %s', c.relname,
              (xpath('/table/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM public.%I', c.relname), false, false, '')))[1]::text)
FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' ORDER BY c.relname;
SQL
}

if [[ "${1:-}" == "--db" ]]; then
    target_db="${2:?укажите базу: --db feelings}"
    [[ "${target_db}" =~ ^[a-z_][a-z0-9_]*$ ]] || { echo "имя базы: только a-z 0-9 _" >&2; exit 64; }
    echo "проверка базы ${target_db} (без восстановления)"
    check_db "${target_db}"
    echo "база ${target_db}: проверки пройдены"
    exit 0
fi

dump="${1:-}"
if [[ -z "${dump}" ]]; then
    dump="$(find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'feelings-*.dump' | sort | tail -n 1)"
fi
if [[ -z "${dump}" || ! -f "${dump}" ]]; then
    echo "нет копии для проверки (${BACKUP_DIR})" >&2
    exit 1
fi

db="feelings_restore_check_$(date -u +%Y%m%d%H%M%S)_$$"
cleanup() { psql_pg -d postgres -c "DROP DATABASE IF EXISTS ${db} WITH (FORCE)" >/dev/null || true; }
trap cleanup EXIT

echo "проверка копии: ${dump} → временная база ${db}"
psql_pg -d postgres -c "CREATE DATABASE ${db}"
# Как при восстановлении: владелец, права уровня базы, vector — до pg_restore. Пароль владельца
# не меняется (keep_owner_password); настройки роли во временной базе удаляются вместе с ней.
psql_pg -d "${db}" -v keep_owner_password=1 -f - < "${INIT_OWNER_SQL}"
"${COMPOSE[@]}" exec -T postgres pg_restore -U postgres -d "${db}" --exit-on-error < "${dump}"
check_db "${db}"
echo "восстановление проверено; временная база удаляется"

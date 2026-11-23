#!/usr/bin/env bash
# Проверка восстановления из копии (ТЗ §10: раз в квартал; задача 0.8). Копия разворачивается
# во ВРЕМЕННУЮ базу того же сервера PostgreSQL, проверяется и удаляется; рабочая база
# feelings не затрагивается.
#   infra/deploy/restore-check.sh [файл.dump]      (по умолчанию — самая свежая копия)
# Проверки: pg_restore без ошибок; версия схемы (alembic_version) есть; таблицы на месте и
# число строк читается; расширение vector и политики RLS восстановлены. Печатает только
# имена таблиц и числа строк — без содержимого.
# Переменные: FEELINGS_BACKUP_DIR, FEELINGS_DEPLOY_ENV_FILE (как в backup.sh).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="${FEELINGS_BACKUP_DIR:-/opt/feelings/backups}"
COMPOSE=(docker compose -f "${HERE}/docker-compose.prod.yml")
if [[ -n "${FEELINGS_DEPLOY_ENV_FILE:-}" ]]; then
    COMPOSE+=(--env-file "${FEELINGS_DEPLOY_ENV_FILE}")
fi

dump="${1:-}"
if [[ -z "${dump}" ]]; then
    dump="$(find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'feelings-*.dump' | sort | tail -n 1)"
fi
if [[ -z "${dump}" || ! -f "${dump}" ]]; then
    echo "нет копии для проверки (${BACKUP_DIR})" >&2
    exit 1
fi

db="feelings_restore_check_$(date -u +%Y%m%d%H%M%S)"
psql_pg() { "${COMPOSE[@]}" exec -T postgres psql -X -q -v ON_ERROR_STOP=1 -U postgres "$@"; }
cleanup() { psql_pg -d postgres -c "DROP DATABASE IF EXISTS ${db} WITH (FORCE)" >/dev/null || true; }
trap cleanup EXIT

echo "проверка копии: ${dump} → временная база ${db}"
psql_pg -d postgres -c "CREATE DATABASE ${db} OWNER feelings_owner"
"${COMPOSE[@]}" exec -T postgres pg_restore -U postgres -d "${db}" --exit-on-error < "${dump}"

psql_pg -d "${db}" -At <<'SQL'
DO $$
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
END $$;
SELECT 'схема: ' || version_num FROM alembic_version;
SELECT format('%s: %s', c.relname,
              (xpath('/table/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM public.%I', c.relname), false, false, '')))[1]::text)
FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' ORDER BY c.relname;
SQL
echo "восстановление проверено; временная база удаляется"

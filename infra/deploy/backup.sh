#!/usr/bin/env bash
# Резервная копия БД тестового стенда (задача 0.8; до автоматизации в 4.7).
#   infra/deploy/backup.sh
# pg_dump -Fc базы feelings из контейнера postgres → $FEELINGS_BACKUP_DIR (по умолчанию
# /opt/feelings/backups), файл feelings-ГГГГ-ММ-ДД-ЧЧММСС.dump с правами 600; проверка, что
# архив читается (pg_restore --list); удаление копий старше 30 дней (ТЗ §10, §9: удалённое
# пользователем уходит и из копий в течение 30 дней).
# Секретов не печатает: дамп снимается через локальный сокет внутри контейнера.
# Переменные: FEELINGS_BACKUP_DIR, FEELINGS_BACKUP_KEEP_DAYS (30), FEELINGS_DEPLOY_ENV_FILE
# (другой .env для compose — локальная проверка).
set -euo pipefail
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="${FEELINGS_BACKUP_DIR:-/opt/feelings/backups}"
KEEP_DAYS="${FEELINGS_BACKUP_KEEP_DAYS:-30}"
COMPOSE=(docker compose -f "${HERE}/docker-compose.prod.yml")
if [[ -n "${FEELINGS_DEPLOY_ENV_FILE:-}" ]]; then
    COMPOSE+=(--env-file "${FEELINGS_DEPLOY_ENV_FILE}")
fi

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"
stamp="$(date -u +%Y-%m-%d-%H%M%S)"
target="${BACKUP_DIR}/feelings-${stamp}.dump"
partial="${target}.partial"
trap 'rm -f "${partial}"' EXIT

"${COMPOSE[@]}" exec -T postgres pg_dump -U postgres -d feelings -Fc > "${partial}"
# Архив читается целиком (оглавление) — иначе копия не засчитывается.
"${COMPOSE[@]}" exec -T postgres pg_restore --list < "${partial}" > /dev/null
chmod 600 "${partial}"
mv "${partial}" "${target}"
echo "копия: ${target} ($(du -h "${target}" | cut -f1))"

find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'feelings-*.dump' -mtime "+$((KEEP_DAYS - 1))" -print -delete \
    | sed 's/^/удалена (старше '"${KEEP_DAYS}"' дн.): /'

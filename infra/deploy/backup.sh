#!/usr/bin/env bash
# Резервная копия БД тестового стенда (задача 0.8; до автоматизации в 4.7).
#   infra/deploy/backup.sh
# pg_dump -Fc базы feelings из контейнера postgres → $FEELINGS_BACKUP_DIR (по умолчанию
# /opt/feelings/backups), файл feelings-ГГГГ-ММ-ДД-ЧЧММСС-<наносекунды>.dump с правами 600;
# проверка, что архив читается (pg_restore --list); удаление старых копий (ТЗ §10, §7: удалённое
# пользователем уходит и из копий в течение 30 дней).
#
# Срок хранения FEELINGS_BACKUP_KEEP_DAYS (по умолчанию 30, допустимо 2…30): копия удаляется
# на запуске, когда ей больше (KEEP_DAYS − 1) суток без 2 часов. При ежедневном запуске в одно
# время (cron, docs/DEPLOY.md §10) ни одна копия не доживает до KEEP_DAYS суток — граница 30 дней
# соблюдается с запасом на сдвиг времени запуска; пропущенный запуск (сервер выключен) удаляет
# просроченное при следующем. Больше 30 нельзя: ТЗ §7.
#
# Одновременно — только один запуск (flock на $FEELINGS_BACKUP_DIR/.backup.lock): ручной запуск
# перед обновлением ждёт копию из cron (до 30 минут), а не пишет параллельно.
# Секретов не печатает: дамп снимается через локальный сокет внутри контейнера.
# Переменные: FEELINGS_BACKUP_DIR, FEELINGS_BACKUP_KEEP_DAYS (30), FEELINGS_DEPLOY_ENV_FILE
# (другой .env для compose — локальная проверка).
set -euo pipefail
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="${FEELINGS_BACKUP_DIR:-/opt/feelings/backups}"
KEEP_DAYS="${FEELINGS_BACKUP_KEEP_DAYS:-30}"
if [[ ! "${KEEP_DAYS}" =~ ^[0-9]+$ ]] || (( 10#${KEEP_DAYS} < 2 || 10#${KEEP_DAYS} > 30 )); then
    echo "FEELINGS_BACKUP_KEEP_DAYS=${KEEP_DAYS}: нужно целое от 2 до 30 (ТЗ §7 — не дольше 30 дней)" >&2
    exit 64
fi
KEEP_DAYS=$((10#${KEEP_DAYS}))
COMPOSE=(docker compose -f "${HERE}/docker-compose.prod.yml")
if [[ -n "${FEELINGS_DEPLOY_ENV_FILE:-}" ]]; then
    COMPOSE+=(--env-file "${FEELINGS_DEPLOY_ENV_FILE}")
fi

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"
exec 9>"${BACKUP_DIR}/.backup.lock"
if ! flock -w 1800 9; then
    echo "другая копия всё ещё снимается (${BACKUP_DIR}/.backup.lock) — запуск отменён" >&2
    exit 75
fi

# Имя уникально: время до наносекунд (сортируется по времени), при совпадении — отказ.
stamp="$(date -u +%Y-%m-%d-%H%M%S-%N)"
target="${BACKUP_DIR}/feelings-${stamp}.dump"
partial="${target}.partial"
if [[ -e "${target}" || -e "${partial}" ]]; then
    echo "файл ${target} уже есть — запуск отменён" >&2
    exit 73
fi
trap 'rm -f "${partial}"' EXIT

"${COMPOSE[@]}" exec -T postgres pg_dump -U postgres -d feelings -Fc > "${partial}"
# Архив читается целиком (оглавление) — иначе копия не засчитывается.
"${COMPOSE[@]}" exec -T postgres pg_restore --list < "${partial}" > /dev/null
chmod 600 "${partial}"
mv -n "${partial}" "${target}"
echo "копия: ${target} ($(du -h "${target}" | cut -f1))"

max_age_min=$(( (KEEP_DAYS - 1) * 1440 - 120 ))
find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'feelings-*.dump' -mmin "+${max_age_min}" -print -delete \
    | sed 's/^/удалена (срок хранения '"${KEEP_DAYS}"' дн.): /'

#!/usr/bin/env bash
# Подготовка сервера тестового стенда «Чувства AI» (задача 0.8). Ubuntu 22.04 / 24.04.
# Запуск от root ОДИН раз (повторный запуск безопасен — шаги идемпотентны):
#
#   sudo bash infra/deploy/bootstrap.sh
#
# Что делает:
#   1. обновляет индекс пакетов, ставит ca-certificates, curl, git, ufw, unattended-upgrades;
#   2. ставит Docker Engine и плагин compose из официального репозитория Docker;
#   3. брандмауэр ufw: входящие запрещены, кроме 22/tcp (SSH), 80/tcp, 443/tcp, 443/udp;
#   4. пользователь `feelings` (без пароля, вход только по ключу SSH — ключ добавляет оператор),
#      член группы docker;
#   5. каталог /opt/feelings (владелец feelings, права 750) — сюда клонируется репозиторий.
#
# Секретов не содержит и не печатает. Ничего не запускает из приложения.
# Важно: членство в группе docker равносильно root на этом сервере — ключ SSH пользователя
# feelings хранить как ключ администратора.
set -euo pipefail

APP_USER="feelings"
APP_DIR="/opt/feelings"

log() { printf '==> %s\n' "$*"; }

if [[ ${EUID} -ne 0 ]]; then
    echo "запустите от root: sudo bash $0" >&2
    exit 1
fi

# shellcheck source=/dev/null
. /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]] || [[ "${VERSION_ID:-}" != "22.04" && "${VERSION_ID:-}" != "24.04" ]]; then
    echo "поддерживаются Ubuntu 22.04 и 24.04 (обнаружено: ${PRETTY_NAME:-неизвестно})" >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive

log "пакеты"
apt-get update -q
apt-get install -y -q ca-certificates curl git ufw unattended-upgrades

log "репозиторий Docker"
install -m 0755 -d /etc/apt/keyrings
if [[ ! -s /etc/apt/keyrings/docker.asc ]]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
fi
DOCKER_LIST="/etc/apt/sources.list.d/docker.list"
DOCKER_REPO="deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable"
if [[ ! -f "${DOCKER_LIST}" ]] || [[ "$(cat "${DOCKER_LIST}")" != "${DOCKER_REPO}" ]]; then
    printf '%s\n' "${DOCKER_REPO}" > "${DOCKER_LIST}"
    apt-get update -q
fi

log "Docker Engine и compose"
# Пакеты из дистрибутива конфликтуют с официальными — удалить, если стоят.
for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do
    if dpkg -s "${pkg}" >/dev/null 2>&1; then
        apt-get remove -y -q "${pkg}"
    fi
done
apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

log "брандмауэр ufw: 22/tcp, 80/tcp, 443/tcp, 443/udp"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow 443/udp >/dev/null
ufw --force enable >/dev/null
# Docker публикует порты в обход ufw (свои правила iptables) — поэтому в compose стенда
# опубликованы только 80 и 443 (Caddy), у БД и Redis портов нет совсем.

log "пользователь ${APP_USER}"
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash "${APP_USER}"
fi
passwd -l "${APP_USER}" >/dev/null   # вход по паролю запрещён, только ключ SSH
usermod -aG docker "${APP_USER}"
install -d -m 0700 -o "${APP_USER}" -g "${APP_USER}" "/home/${APP_USER}/.ssh"

log "каталог ${APP_DIR}"
install -d -m 0750 -o "${APP_USER}" -g "${APP_USER}" "${APP_DIR}"
install -d -m 0700 -o "${APP_USER}" -g "${APP_USER}" "${APP_DIR}/backups"

log "автоматические обновления безопасности"
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

log "готово"
docker --version
docker compose version
ufw status | sed -n '1,12p'
cat <<EOF

Дальше (docs/DEPLOY.md):
  - добавьте публичный ключ SSH оператора в /home/${APP_USER}/.ssh/authorized_keys (права 600);
  - войдите как ${APP_USER} и клонируйте репозиторий в ${APP_DIR}/app.
EOF

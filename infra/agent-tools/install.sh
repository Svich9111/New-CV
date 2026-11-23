#!/usr/bin/env bash
# Установка средств экономии токенов для агентов Claude Code: RTK и Serena.
# Идемпотентно; безопасно запускать повторно. Подробности и замеры — docs/AGENT_TOOLS.md.
#
#   RTK    — сжимает вывод команд (git, pytest, ls, find…) до того, как его прочитает агент.
#            Подключается хуком PreToolUse из .claude/settings.json.
#   Serena — MCP-сервер с поиском по символам (функции, классы, ссылки) вместо чтения целых файлов.
#            Подключается из .mcp.json; здесь только прогревается кэш uvx.
set -euo pipefail

RTK_VERSION="${RTK_VERSION:-v1.2.1}"
SERENA_VERSION="${SERENA_VERSION:-2.1.0}"
BIN_DIR="${HOME}/.local/bin"
export PATH="${BIN_DIR}:${HOME}/.cargo/bin:${PATH}"

log() { printf '[agent-tools] %s\n' "$*"; }

# --- RTK -------------------------------------------------------------------------------------
if command -v rtk >/dev/null 2>&1 && rtk --version | grep -q "${RTK_VERSION#v}"; then
  log "rtk ${RTK_VERSION} уже установлен"
else
  log "ставлю rtk ${RTK_VERSION}"
  # 1) готовый бинарник с проверкой контрольной суммы (нужен доступ к github.com/releases);
  # 2) иначе — сборка из исходников тега через cargo (~4 мин), git-доступ к GitHub есть всегда.
  if ! curl -fsSL "https://raw.githubusercontent.com/rtk-ai/rtk/${RTK_VERSION}/install.sh" \
        | RTK_VERSION="${RTK_VERSION}" RTK_INSTALL_DIR="${BIN_DIR}" sh; then
    log "готовый бинарник недоступен — собираю из исходников"
    cargo install --git https://github.com/rtk-ai/rtk --tag "${RTK_VERSION}" --locked \
      --root "${HOME}/.local" rtk
  fi
fi

# Настройки RTK живут только на уровне пользователя (~/.config/rtk/config.toml).
# Исключения — команды, где фильтр RTK меняет результат в этом проекте (см. docs/AGENT_TOOLS.md):
#   npm run lint → «rtk lint» запускает только eslint и теряет «&& tsc -b»; падает на разборе JSON;
#   tsc/npx tsc  → «rtk tsc» берёт глобальный TypeScript вместо версии проекта — ложные ошибки.
RTK_CONFIG="${XDG_CONFIG_HOME:-${HOME}/.config}/rtk/config.toml"
mkdir -p "$(dirname "${RTK_CONFIG}")"
cat > "${RTK_CONFIG}" <<'TOML'
# Создано infra/agent-tools/install.sh — правки вносить там.
[telemetry]
enabled = false

[hooks]
exclude_commands = ["npm run lint", "tsc", "npx tsc", "eslint", "npx eslint"]
suppress_hook_warning = true
TOML
log "конфиг rtk: ${RTK_CONFIG}"

# --- Serena ----------------------------------------------------------------------------------
# Прогреваем кэш uvx той же командой, что в .mcp.json, чтобы первый запуск сервера не упирался
# в тайм-аут MCP.
log "прогреваю serena-agent ${SERENA_VERSION}"
uvx -q -p 3.13 --from "serena-agent==${SERENA_VERSION}" serena --version

log "готово: $(rtk --version), Serena ${SERENA_VERSION}"

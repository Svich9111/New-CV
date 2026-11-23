#!/usr/bin/env bash
# Глобальная установка средств экономии токенов для Claude Code: RTK и Serena.
# Действует на ВСЕ проекты пользователя на этой машине. Идемпотентно; можно запускать повторно.
# Подробности и замеры — docs/AGENT_TOOLS.md. Linux и macOS.
#
#   RTK    — сжимает вывод команд (git, pytest, ls, find…) до того, как его прочитает агент.
#            Хук PreToolUse в ~/.claude/settings.json.
#   Serena — MCP-сервер с поиском по символам вместо чтения целых файлов.
#            Сервер уровня пользователя в ~/.claude.json, проект — по текущему каталогу.
set -euo pipefail

RTK_VERSION="${RTK_VERSION:-v1.2.1}"
SERENA_VERSION="${SERENA_VERSION:-2.1.0}"
BIN_DIR="${HOME}/.local/bin"
export PATH="${BIN_DIR}:${HOME}/.cargo/bin:${PATH}"

log() { printf '[agent-tools] %s\n' "$*"; }

# --- RTK: бинарник --------------------------------------------------------------------------
if command -v rtk >/dev/null 2>&1 && rtk --version | grep -q "${RTK_VERSION#v}"; then
  log "rtk ${RTK_VERSION} уже установлен"
else
  log "ставлю rtk ${RTK_VERSION}"
  # Готовый бинарник с проверкой контрольной суммы; если недоступен — сборка тега через cargo.
  if ! curl -fsSL "https://raw.githubusercontent.com/rtk-ai/rtk/${RTK_VERSION}/install.sh" \
        | RTK_VERSION="${RTK_VERSION}" RTK_INSTALL_DIR="${BIN_DIR}" sh; then
    log "готовый бинарник недоступен — собираю из исходников (~4 мин)"
    cargo install --git https://github.com/rtk-ai/rtk --tag "${RTK_VERSION}" --locked \
      --root "${HOME}/.local" rtk
  fi
fi

# --- RTK: настройки -------------------------------------------------------------------------
# Исключения — команды, где фильтр RTK меняет результат (см. docs/AGENT_TOOLS.md):
#   npm run lint → «rtk lint» запускает только eslint и теряет остальную часть скрипта;
#   tsc/npx tsc  → «rtk tsc» берёт глобальный TypeScript вместо версии проекта.
RTK_CONFIG="${XDG_CONFIG_HOME:-${HOME}/.config}/rtk/config.toml"
mkdir -p "$(dirname "${RTK_CONFIG}")"
cat > "${RTK_CONFIG}" <<'TOML'
# Создано infra/agent-tools/install.sh (репозиторий New-CV) — правки вносить там.
[telemetry]
enabled = false

[hooks]
exclude_commands = ["npm run lint", "tsc", "npx tsc", "eslint", "npx eslint"]
suppress_hook_warning = true
TOML

# --- RTK: хук Claude Code для всех проектов -------------------------------------------------
# Сам дописывает хук в ~/.claude/settings.json, не трогая остальные настройки (делает .bak).
rtk init -g --auto-patch --hook-only >/dev/null
log "хук rtk: ~/.claude/settings.json"

# --- Serena: MCP-сервер для всех проектов ---------------------------------------------------
SERENA_ARGS=(uvx -q -p 3.13 --from "serena-agent==${SERENA_VERSION}" serena start-mcp-server
  --context claude-code --project-from-cwd --agent-interface tools --enable-web-dashboard false)
uvx -q -p 3.13 --from "serena-agent==${SERENA_VERSION}" serena --version >/dev/null  # прогрев кэша
if command -v claude >/dev/null 2>&1; then
  claude mcp remove --scope user serena >/dev/null 2>&1 || true
  claude mcp add --scope user serena -e SERENA_USAGE_REPORTING=false -- "${SERENA_ARGS[@]}" >/dev/null
  log "serena: MCP-сервер уровня пользователя (~/.claude.json)"
else
  log "claude не найден — пропускаю регистрацию Serena; запустите скрипт после установки Claude Code"
fi

# Serena создаёт .serena/ в каждом проекте — убираем из git во всех репозиториях.
GIT_IGNORE="$(git config --global core.excludesfile || true)"
GIT_IGNORE="${GIT_IGNORE/#\~/$HOME}"
GIT_IGNORE="${GIT_IGNORE:-${XDG_CONFIG_HOME:-${HOME}/.config}/git/ignore}"
mkdir -p "$(dirname "${GIT_IGNORE}")" && touch "${GIT_IGNORE}"
grep -qxF '.serena/' "${GIT_IGNORE}" || echo '.serena/' >> "${GIT_IGNORE}"

log "готово: $(rtk --version), Serena ${SERENA_VERSION}. Перезапустите Claude Code."

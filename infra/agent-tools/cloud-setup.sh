# Блок для скрипта запуска облачного окружения Claude Code (Settings → окружение → Setup script).
# Ставит RTK и Serena глобально — на все проекты сессии. Самодостаточен: не нужен клон New-CV.
# Синхронизировать с install.sh при изменении версий или исключений.
( set -e
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! command -v rtk >/dev/null 2>&1; then
  curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/v1.2.1/install.sh | RTK_VERSION=v1.2.1 sh \
    || cargo install --git https://github.com/rtk-ai/rtk --tag v1.2.1 --locked --root "$HOME/.local" rtk
fi
mkdir -p "$HOME/.config/rtk" "$HOME/.config/git"
printf '[telemetry]\nenabled = false\n\n[hooks]\nexclude_commands = ["npm run lint", "tsc", "npx tsc", "eslint", "npx eslint"]\nsuppress_hook_warning = true\n' > "$HOME/.config/rtk/config.toml"
rtk init -g --auto-patch --hook-only >/dev/null
uvx -q -p 3.13 --from serena-agent==2.1.0 serena --version
claude mcp remove --scope user serena >/dev/null 2>&1 || true
claude mcp add --scope user serena -e SERENA_USAGE_REPORTING=false -- \
  uvx -q -p 3.13 --from serena-agent==2.1.0 serena start-mcp-server --context claude-code --project-from-cwd --agent-interface tools --enable-web-dashboard false
touch "$HOME/.config/git/ignore"; grep -qxF '.serena/' "$HOME/.config/git/ignore" || echo '.serena/' >> "$HOME/.config/git/ignore"
) || echo "agent-tools: setup failed, continuing without RTK/Serena"

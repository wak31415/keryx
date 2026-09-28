#!/usr/bin/env bash
# Install (or remove) the launchd agents that keep Jarvis and its tunnel running on
# macOS. (Linux uses scripts/install-systemd.sh instead.)
#
#   scripts/install-launchd.sh              # render the templates and load both agents
#   scripts/install-launchd.sh --uninstall  # unload both agents and delete the plists
#
# The templates in ops/launchd/ carry __PLACEHOLDER__ names; this script fills them in
# from `command -v` and `jarvis config get`, writes the result to ~/Library/LaunchAgents/, and hands them
# to launchctl. The tunnel agent runs ngrok here rather than cloudflared: a reserved ngrok
# domain needs no DNS zone, which is the right trade on a laptop. Logs land in
# STATE_DIR/logs/ (~/.local/state/jarvis/logs/ unless the configuration says otherwise), and
# the agents get this shell's PATH, JARVIS_HOME and XDG directories — run it from the shell
# whose tools the subagents should have, and again after that changes.
#
# The scaffolding every installer needs — argument parsing, reading the configuration, the PATH checks,
# template rendering — is in scripts/lib.sh.
set -euo pipefail

# shellcheck source=scripts/lib.sh
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"

TEMPLATES="$REPO/ops/launchd"
AGENTS="$HOME/Library/LaunchAgents"
LABELS=(dev.jarvis.agent dev.jarvis.tunnel)
# Labels these agents used before 2026-09-02. They are booted out on install as well as on
# --uninstall, so an install predating the rename does not survive as a second copy of the
# same service, with KeepAlive, fighting over the same port.
LEGACY_LABELS=(com.william.jarvis com.william.ngrok)

remove_agents() {
  # remove_agents LABEL... — unload and delete each one that is actually installed.
  for label in "$@"; do
    [[ -f "$AGENTS/$label.plist" ]] || continue
    launchctl bootout "gui/$UID/$label" 2>/dev/null || true
    rm -f "$AGENTS/$label.plist"
    echo "removed $label"
  done
}

parse_install_args "$@"
if (( UNINSTALL )); then
  remove_agents "${LABELS[@]}" "${LEGACY_LABELS[@]}"
  exit 0
fi

require_public_host "use a reserved ngrok domain"
require_command UV uv "https://docs.astral.sh/uv/"
require_command NGROK ngrok "brew install ngrok"

make_dirs "$AGENTS"
remove_agents "${LEGACY_LABELS[@]}"

for label in "${LABELS[@]}"; do
  plist="$AGENTS/$label.plist"
  render "$TEMPLATES/$label.plist" "$plist" \
    "UV=$UV" "NGROK=$NGROK" "PUBLIC_HOST=$PUBLIC_HOST" "PORT=$PORT" \
    "PATH=$(xml_escape "$PATH")" "LOGS=$(xml_escape "$LOGS")" \
    "JARVIS_HOME=$(xml_escape "$JARVIS_HOME_DIR")" \
    "XDG_CONFIG_HOME=$(xml_escape "$RESOLVED_XDG_CONFIG_HOME")" \
    "XDG_DATA_HOME=$(xml_escape "$RESOLVED_XDG_DATA_HOME")" \
    "XDG_STATE_HOME=$(xml_escape "$RESOLVED_XDG_STATE_HOME")" \
    "XDG_CACHE_HOME=$(xml_escape "$RESOLVED_XDG_CACHE_HOME")"
  # A previous version may still be loaded; booting it out first makes this re-runnable.
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID" "$plist"
  echo "loaded $label ($plist)"
done

cat <<INFO

Jarvis is running under launchd.

  status:  launchctl print gui/$UID/dev.jarvis.agent | head -20
  logs:    tail -f $LOGS/jarvis.err.log $LOGS/jarvis.log
  stop:    $0 --uninstall
INFO
webhook_hint

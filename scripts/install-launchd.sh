#!/usr/bin/env bash
# Install (or remove) the launchd agents that keep Keryx and its tunnel running on
# macOS. (Linux uses scripts/install-systemd.sh instead.)
#
#   scripts/install-launchd.sh              # render the templates and load both agents
#   scripts/install-launchd.sh --uninstall  # unload both agents and delete the plists
#   scripts/install-launchd.sh --llm --voice  # the local model servers instead (either one;
#                                             # with --uninstall, remove them)
#
# The templates in ops/launchd/ carry __PLACEHOLDER__ names; this script fills them in
# from `command -v` and `keryx config get`, writes the result to ~/Library/LaunchAgents/, and hands them
# to launchctl. The tunnel agent runs ngrok here rather than cloudflared: a reserved ngrok
# domain needs no DNS zone, which is the right trade on a laptop. Logs land in
# STATE_DIR/logs/ (~/.local/state/keryx/logs/ unless the configuration says otherwise), and
# the agents get this shell's PATH, KERYX_HOME and XDG directories — run it from the shell
# whose tools the subagents should have, and again after that changes.
#
# The scaffolding every installer needs — argument parsing, reading the configuration, the PATH checks,
# template rendering — is in scripts/lib.sh.
set -euo pipefail

# shellcheck source=scripts/lib.sh
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"

TEMPLATES="$REPO/ops/launchd"
AGENTS="$HOME/Library/LaunchAgents"
LABELS=(dev.keryx.agent dev.keryx.tunnel)

remove_agents() {
  # remove_agents LABEL... — delete every plist first, then unload each job. In that order:
  # bootout waits for a job to exit (up to its ExitTimeOut), and an uninstall cut short
  # there — the app's quit deadline, a shutdown — must not leave any RunAtLoad/KeepAlive
  # plist behind to start at the next login, the later labels' included. The bootout is
  # asked even with no plist left, so a second run finishes what an interrupted one began.
  for label in "$@"; do
    if [[ -f "$AGENTS/$label.plist" ]]; then
      rm -f "$AGENTS/$label.plist"
      echo "removed $label"
    fi
  done
  for label in "$@"; do
    launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  done
}

load_agent() {
  # load_agent LABEL PLIST — (re)load one job. Enabled first: `keryx service stop` disables
  # it, and launchd refuses to bootstrap a disabled job.
  launchctl bootout "gui/$UID/$1" 2>/dev/null || true
  launchctl enable "gui/$UID/$1"
  launchctl bootstrap "gui/$UID" "$2"
}

parse_install_args "$@"
if (( LLM || VOICE )); then
  LABELS=()
  (( LLM )) && LABELS+=(dev.keryx.llm)
  (( VOICE )) && LABELS+=(dev.keryx.voice)
fi
if (( UNINSTALL )); then
  remove_agents "${LABELS[@]}"
  exit 0
fi

if (( LLM || VOICE )); then
  # The local model servers: no tunnel, no public host. `keryx models serve` reads the rest.
  require_command UV uv "https://docs.astral.sh/uv/"
  make_dirs "$AGENTS"
  for label in "${LABELS[@]}"; do
    plist="$AGENTS/$label.plist"
    render "$TEMPLATES/$label.plist" "$plist" \
      "UV=$UV" "PATH=$(xml_escape "$PATH")" "LOGS=$(xml_escape "$LOGS")" \
      "KERYX_HOME=$(xml_escape "$KERYX_HOME_DIR")" \
      "XDG_CONFIG_HOME=$(xml_escape "$RESOLVED_XDG_CONFIG_HOME")" \
      "XDG_DATA_HOME=$(xml_escape "$RESOLVED_XDG_DATA_HOME")" \
      "XDG_STATE_HOME=$(xml_escape "$RESOLVED_XDG_STATE_HOME")" \
      "XDG_CACHE_HOME=$(xml_escape "$RESOLVED_XDG_CACHE_HOME")"
    load_agent "$label" "$plist"
    echo "loaded $label ($plist)"
  done
  exit 0
fi

require_public_host "use a reserved ngrok domain"
require_command UV uv "https://docs.astral.sh/uv/"
require_command NGROK ngrok "brew install ngrok"

make_dirs "$AGENTS"

for label in "${LABELS[@]}"; do
  plist="$AGENTS/$label.plist"
  render "$TEMPLATES/$label.plist" "$plist" \
    "UV=$UV" "NGROK=$NGROK" "PUBLIC_HOST=$PUBLIC_HOST" "PORT=$PORT" \
    "PATH=$(xml_escape "$PATH")" "LOGS=$(xml_escape "$LOGS")" \
    "KERYX_HOME=$(xml_escape "$KERYX_HOME_DIR")" \
    "XDG_CONFIG_HOME=$(xml_escape "$RESOLVED_XDG_CONFIG_HOME")" \
    "XDG_DATA_HOME=$(xml_escape "$RESOLVED_XDG_DATA_HOME")" \
    "XDG_STATE_HOME=$(xml_escape "$RESOLVED_XDG_STATE_HOME")" \
    "XDG_CACHE_HOME=$(xml_escape "$RESOLVED_XDG_CACHE_HOME")"
  # A previous version may still be loaded; booting it out first makes this re-runnable.
  load_agent "$label" "$plist"
  echo "loaded $label ($plist)"
done

cat <<INFO

Keryx is running under launchd.

  status:  launchctl print gui/$UID/dev.keryx.agent | head -20
  logs:    tail -f $LOGS/keryx.err.log $LOGS/keryx.log
  stop:    $0 --uninstall
INFO
webhook_hint

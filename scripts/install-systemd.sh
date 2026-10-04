#!/usr/bin/env bash
# Install (or remove) the systemd user units that keep Keryx and its Cloudflare tunnel
# running on a Linux host. (macOS uses scripts/install-launchd.sh instead.)
#
#   scripts/install-systemd.sh              # render the templates and start both units
#   scripts/install-systemd.sh --uninstall  # stop both units and delete them
#   scripts/install-systemd.sh --llm --voice  # the local model servers instead (either one;
#                                             # with --uninstall, remove them)
#
# The templates in ops/systemd/ carry __PLACEHOLDER__ names; this script fills them in from
# `command -v` and `keryx config get`, writes the result to ~/.config/systemd/user/, and hands
# them to systemctl. Lingering keeps both running when nobody is logged in, so the machine
# answers the phone after a reboot. Logs land in STATE_DIR/logs/ (~/.local/state/keryx/logs/
# unless the configuration says otherwise), and the units get this shell's PATH, KERYX_HOME
# and XDG directories — run it from the shell whose tools the subagents should have, and again
# after that changes.
#
# The scaffolding every installer needs — argument parsing, reading the configuration, the PATH checks,
# template rendering — is in scripts/lib.sh.
set -euo pipefail

# shellcheck source=scripts/lib.sh
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"

TEMPLATES="$REPO/ops/systemd"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICES=(keryx cloudflared)

uninstall() {
  for name in "${SERVICES[@]}"; do
    systemctl --user disable --now "$name.service" 2>/dev/null || true
    rm -f "$UNITS/$name.service"
    echo "removed $name.service"
  done
  systemctl --user daemon-reload
}

parse_install_args "$@"
if (( LLM || VOICE )); then
  SERVICES=()
  (( LLM )) && SERVICES+=(keryx-llm)
  (( VOICE )) && SERVICES+=(keryx-voice)
fi
if (( UNINSTALL )); then
  uninstall
  exit 0
fi

if (( LLM || VOICE )); then
  # The local model servers: no tunnel, no public host. `keryx models serve` reads the rest.
  require_command UV uv "https://docs.astral.sh/uv/"
  make_dirs "$UNITS"
  for name in "${SERVICES[@]}"; do
    render "$TEMPLATES/$name.service" "$UNITS/$name.service" \
      "UV=$UV" "PATH=$(systemd_quoted "$PATH")" "LOGS=$(systemd_path "$LOGS")" \
      "KERYX_HOME=$(systemd_quoted "$KERYX_HOME_DIR")" \
      "XDG_CONFIG_HOME=$(systemd_quoted "$RESOLVED_XDG_CONFIG_HOME")" \
      "XDG_DATA_HOME=$(systemd_quoted "$RESOLVED_XDG_DATA_HOME")" \
      "XDG_STATE_HOME=$(systemd_quoted "$RESOLVED_XDG_STATE_HOME")" \
      "XDG_CACHE_HOME=$(systemd_quoted "$RESOLVED_XDG_CACHE_HOME")"
  done
  systemctl --user daemon-reload
  for name in "${SERVICES[@]}"; do
    systemctl --user enable "$name.service"
    systemctl --user restart "$name.service"
    echo "started $name.service ($UNITS/$name.service)"
  done
  loginctl enable-linger "$USER" 2>/dev/null || true
  echo "logs: tail -f $LOGS/keryx-llm.err.log $LOGS/keryx-voice.err.log"
  exit 0
fi

require_public_host "the hostname routed to the Cloudflare tunnel"
TUNNEL="$(config_value CLOUDFLARE_TUNNEL)"
TUNNEL="${TUNNEL:-keryx}"

require_command UV uv "https://docs.astral.sh/uv/"
require_command CLOUDFLARED cloudflared "https://developers.cloudflare.com/cloudflare-one/"
if ! "$CLOUDFLARED" tunnel info "$TUNNEL" >/dev/null 2>&1; then
  cat >&2 <<HINT
no Cloudflare tunnel named "$TUNNEL" on this machine. Create it once (opens a browser):

  cloudflared tunnel login
  cloudflared tunnel create $TUNNEL
  cloudflared tunnel route dns $TUNNEL $PUBLIC_HOST

To use a different name: keryx config set CLOUDFLARE_TUNNEL <name>
HINT
  exit 1
fi

make_dirs "$UNITS"

for name in "${SERVICES[@]}"; do
  render "$TEMPLATES/$name.service" "$UNITS/$name.service" \
    "UV=$UV" "CLOUDFLARED=$CLOUDFLARED" "TUNNEL=$TUNNEL" "PORT=$PORT" \
    "PATH=$(systemd_quoted "$PATH")" "LOGS=$(systemd_path "$LOGS")" \
    "KERYX_HOME=$(systemd_quoted "$KERYX_HOME_DIR")" \
    "XDG_CONFIG_HOME=$(systemd_quoted "$RESOLVED_XDG_CONFIG_HOME")" \
    "XDG_DATA_HOME=$(systemd_quoted "$RESOLVED_XDG_DATA_HOME")" \
    "XDG_STATE_HOME=$(systemd_quoted "$RESOLVED_XDG_STATE_HOME")" \
    "XDG_CACHE_HOME=$(systemd_quoted "$RESOLVED_XDG_CACHE_HOME")"
done
systemctl --user daemon-reload
for name in "${SERVICES[@]}"; do
  systemctl --user enable --now "$name.service"
  echo "started $name.service ($UNITS/$name.service)"
done

# Without linger, the user manager stops at logout and takes both units with it.
if ! loginctl enable-linger "$USER" 2>/dev/null; then
  echo "could not enable linger — run: sudo loginctl enable-linger $USER" >&2
fi

cat <<INFO

Keryx is running under systemd.

  status:  systemctl --user status keryx cloudflared
  logs:    journalctl --user -u keryx -f
           tail -f $LOGS/keryx.err.log $LOGS/keryx.log
  stop:    $0 --uninstall
INFO
webhook_hint

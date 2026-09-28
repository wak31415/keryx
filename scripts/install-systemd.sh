#!/usr/bin/env bash
# Install (or remove) the systemd user units that keep Jarvis and its Cloudflare tunnel
# running on a Linux host. (macOS uses scripts/install-launchd.sh instead.)
#
#   scripts/install-systemd.sh              # render the templates and start both units
#   scripts/install-systemd.sh --uninstall  # stop both units and delete them
#
# The templates in ops/systemd/ carry __PLACEHOLDER__ names; this script fills them in from
# `command -v` and `jarvis config get`, writes the result to ~/.config/systemd/user/, and hands
# them to systemctl. Lingering keeps both running when nobody is logged in, so the machine
# answers the phone after a reboot. Logs land in DATA_DIR/logs/ (~/.jarvis/logs/ unless the
# configuration says otherwise), and the units get this shell's PATH — run it from the shell
# whose tools the subagents should have, and again after that changes.
#
# The scaffolding every installer needs — argument parsing, reading the configuration, the PATH checks,
# template rendering — is in scripts/lib.sh.
set -euo pipefail

# shellcheck source=scripts/lib.sh
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"

TEMPLATES="$REPO/ops/systemd"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICES=(jarvis cloudflared)

uninstall() {
  for name in "${SERVICES[@]}"; do
    systemctl --user disable --now "$name.service" 2>/dev/null || true
    rm -f "$UNITS/$name.service"
    echo "removed $name.service"
  done
  systemctl --user daemon-reload
}

parse_install_args "$@"
if (( UNINSTALL )); then
  uninstall
  exit 0
fi

require_public_host "the hostname routed to the Cloudflare tunnel"
TUNNEL="$(config_value CLOUDFLARE_TUNNEL)"
TUNNEL="${TUNNEL:-jarvis}"

require_command UV uv "https://docs.astral.sh/uv/"
require_command CLOUDFLARED cloudflared "https://developers.cloudflare.com/cloudflare-one/"
if ! "$CLOUDFLARED" tunnel info "$TUNNEL" >/dev/null 2>&1; then
  cat >&2 <<HINT
no Cloudflare tunnel named "$TUNNEL" on this machine. Create it once (opens a browser):

  cloudflared tunnel login
  cloudflared tunnel create $TUNNEL
  cloudflared tunnel route dns $TUNNEL $PUBLIC_HOST

To use a different name: jarvis config set CLOUDFLARE_TUNNEL <name>
HINT
  exit 1
fi

make_dirs "$UNITS"

for name in "${SERVICES[@]}"; do
  render "$TEMPLATES/$name.service" "$UNITS/$name.service" \
    "UV=$UV" "CLOUDFLARED=$CLOUDFLARED" "TUNNEL=$TUNNEL" "PORT=$PORT" \
    "PATH=$(systemd_quoted "$PATH")" "LOGS=$(systemd_path "$LOGS")" \
    "JARVIS_HOME=$(systemd_quoted "$JARVIS_HOME_DIR")"
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

Jarvis is running under systemd.

  status:  systemctl --user status jarvis cloudflared
  logs:    journalctl --user -u jarvis -f
           tail -f $LOGS/jarvis.err.log $LOGS/jarvis.log
  stop:    $0 --uninstall
INFO
webhook_hint

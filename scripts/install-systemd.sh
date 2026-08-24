#!/usr/bin/env bash
# Install (or remove) the systemd user units that keep Jarvis and its Cloudflare tunnel
# running on a Linux host. (macOS uses scripts/install-launchd.sh instead.)
#
#   scripts/install-systemd.sh              # render the templates and start both units
#   scripts/install-systemd.sh --uninstall  # stop both units and delete them
#
# The templates in ops/systemd/ carry __PLACEHOLDER__ names; this script fills them in from
# `command -v` and the env file, writes the result to ~/.config/systemd/user/, and hands
# them to systemctl. Lingering keeps both running when nobody is logged in, so the machine
# answers the phone after a reboot. Logs land in ~/.jarvis/logs/.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib.sh
source "$REPO/scripts/lib.sh"

TEMPLATES="$REPO/ops/systemd"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
LOGS="$HOME/.jarvis/logs"
SERVICES=(jarvis cloudflared)

uninstall() {
  for name in "${SERVICES[@]}"; do
    systemctl --user disable --now "$name.service" 2>/dev/null || true
    rm -f "$UNITS/$name.service"
    echo "removed $name.service"
  done
  systemctl --user daemon-reload
}

if [[ "${1:-}" == "--uninstall" ]]; then
  uninstall
  exit 0
fi
if [[ -n "${1:-}" ]]; then
  echo "usage: $(basename "$0") [--uninstall]" >&2
  exit 2
fi

ENV_FILE="$REPO/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "no env file in $REPO: copy .env.example and fill it in" >&2
  exit 1
fi

PUBLIC_HOST="$(env_value PUBLIC_HOST "$ENV_FILE")"
PORT="$(env_value PORT "$ENV_FILE")"
PORT="${PORT:-8080}"
TUNNEL="$(env_value CLOUDFLARE_TUNNEL "$ENV_FILE")"
TUNNEL="${TUNNEL:-jarvis}"

if [[ -z "$PUBLIC_HOST" ]]; then
  echo "PUBLIC_HOST is not set (the hostname routed to the Cloudflare tunnel)" >&2
  exit 1
fi

UV="$(command -v uv || true)"
CLOUDFLARED="$(command -v cloudflared || true)"
if [[ -z "$UV" ]]; then
  echo "uv is not on PATH (https://docs.astral.sh/uv/)" >&2
  exit 1
fi
if [[ -z "$CLOUDFLARED" ]]; then
  echo "cloudflared is not on PATH (https://developers.cloudflare.com/cloudflare-one/)" >&2
  exit 1
fi
if ! "$CLOUDFLARED" tunnel info "$TUNNEL" >/dev/null 2>&1; then
  cat >&2 <<HINT
no Cloudflare tunnel named "$TUNNEL" on this machine. Create it once (opens a browser):

  cloudflared tunnel login
  cloudflared tunnel create $TUNNEL
  cloudflared tunnel route dns $TUNNEL $PUBLIC_HOST

Set CLOUDFLARE_TUNNEL in the env file to use a different name.
HINT
  exit 1
fi

mkdir -p "$UNITS" "$LOGS"

render() {
  # render <template> <destination>
  sed -e "s|__REPO__|$REPO|g" \
      -e "s|__HOME__|$HOME|g" \
      -e "s|__UV__|$UV|g" \
      -e "s|__CLOUDFLARED__|$CLOUDFLARED|g" \
      -e "s|__TUNNEL__|$TUNNEL|g" \
      -e "s|__PORT__|$PORT|g" \
      "$1" > "$2"
}

for name in "${SERVICES[@]}"; do
  render "$TEMPLATES/$name.service" "$UNITS/$name.service"
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
           tail -f $LOGS/jarvis.err.log $HOME/.jarvis/logs/jarvis.log
  stop:    $0 --uninstall

Point the Twilio number's voice webhook at https://$PUBLIC_HOST/twilio/voice (HTTP POST)
and the status callback at https://$PUBLIC_HOST/twilio/status.
INFO

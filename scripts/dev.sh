#!/usr/bin/env bash
# Dev loop for the phone channel, from a clone: the Cloudflare tunnel plus `keryx serve`.
#
# Point the Twilio number's voice webhook at https://$PUBLIC_HOST/twilio/voice and call in.
# Extra flags go straight through to `keryx serve`, e.g. `scripts/dev.sh --demo`. For the
# service that runs at boot, use `keryx service install` instead.
#
# Settings are read through `keryx config get`, never by grepping a file. KERYX_CLI says how
# to run keryx for that (default `uv run --quiet keryx`); the tests point it elsewhere.
set -euo pipefail

cd "$(dirname "$0")/.."
read -r -a KERYX <<< "${KERYX_CLI:-uv run --quiet keryx}"

PUBLIC_HOST="$("${KERYX[@]}" config get PUBLIC_HOST)"
if [[ -z "$PUBLIC_HOST" ]]; then
  echo "PUBLIC_HOST is not set (the hostname routed to the Cloudflare tunnel):" >&2
  echo "  keryx config set PUBLIC_HOST keryx.example.com" >&2
  exit 1
fi
PORT="$("${KERYX[@]}" config get PORT)"
PORT="${PORT:-8080}"
TUNNEL="$("${KERYX[@]}" config get CLOUDFLARE_TUNNEL)"
TUNNEL="${TUNNEL:-keryx}"
if ! command -v cloudflared >/dev/null; then
  echo "cloudflared is not on PATH (https://developers.cloudflare.com/cloudflare-one/)" >&2
  exit 1
fi
# Beside the service's own logs in STATE_DIR, never in the checkout. keryx quotes the value
# for exactly this eval.
eval "STATE=$("${KERYX[@]}" config path --shell | sed -n 's/^STATE_DIR=//p')"
TUNNEL_LOG="$STATE/logs/cloudflared.log"
(umask 077 && mkdir -p "$STATE/logs")

cloudflared tunnel --no-autoupdate --protocol http2 run \
  --url "http://localhost:$PORT" "$TUNNEL" > "$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!
# No `exec` below: this trap is what stops the tunnel when keryx exits.
trap 'kill "$TUNNEL_PID" 2>/dev/null || true' EXIT

echo "tunnel:  https://$PUBLIC_HOST -> http://localhost:$PORT  (log: $TUNNEL_LOG)"
echo "webhook: https://$PUBLIC_HOST/twilio/voice"

uv run keryx serve "$@"

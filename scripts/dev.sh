#!/usr/bin/env bash
# Dev loop for the phone channel: the Cloudflare tunnel plus `jarvis serve`.
#
# Point the Twilio number's voice webhook at https://$PUBLIC_HOST/twilio/voice and call in.
# Extra flags go straight through to `jarvis serve`, e.g. `scripts/dev.sh --fake-agents`.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh

require_public_host "the hostname routed to the Cloudflare tunnel"
TUNNEL="$(config_value CLOUDFLARE_TUNNEL)"
TUNNEL="${TUNNEL:-jarvis}"
require_command CLOUDFLARED cloudflared "https://developers.cloudflare.com/cloudflare-one/"
make_dirs
# Beside the service's own logs in STATE_DIR, never in the checkout.
TUNNEL_LOG="$LOGS/cloudflared.log"

cloudflared tunnel --no-autoupdate --protocol http2 run \
  --url "http://localhost:$PORT" "$TUNNEL" > "$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!
# No `exec` below: this trap is what stops the tunnel when jarvis exits.
trap 'kill "$TUNNEL_PID" 2>/dev/null || true' EXIT

echo "tunnel:  https://$PUBLIC_HOST -> http://localhost:$PORT  (log: $TUNNEL_LOG)"
echo "webhook: https://$PUBLIC_HOST/twilio/voice"

uv run jarvis serve "$@"

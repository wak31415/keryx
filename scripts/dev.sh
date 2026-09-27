#!/usr/bin/env bash
# Dev loop for the phone channel: the Cloudflare tunnel plus `jarvis serve`.
#
# Point the Twilio number's voice webhook at https://$PUBLIC_HOST/twilio/voice and call in.
# The wake word is off here (the tunnel, not the mic, is what we are exercising); pass
# extra flags straight through, e.g. `scripts/dev.sh --no-phone`.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh

require_public_host "the hostname routed to the Cloudflare tunnel"
TUNNEL="$(config_value CLOUDFLARE_TUNNEL)"
TUNNEL="${TUNNEL:-jarvis}"
require_command CLOUDFLARED cloudflared "https://developers.cloudflare.com/cloudflare-one/"

cloudflared tunnel --no-autoupdate --protocol http2 run \
  --url "http://localhost:$PORT" "$TUNNEL" > .cloudflared.log 2>&1 &
TUNNEL_PID=$!
# No `exec` below: this trap is what stops the tunnel when jarvis exits.
trap 'kill "$TUNNEL_PID" 2>/dev/null || true' EXIT

echo "tunnel:  https://$PUBLIC_HOST -> http://localhost:$PORT  (log: .cloudflared.log)"
echo "webhook: https://$PUBLIC_HOST/twilio/voice"

uv run jarvis serve --no-wakeword "$@"

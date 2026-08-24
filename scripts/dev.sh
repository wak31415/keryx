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

ENV_FILE=".env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "no env file found: copy .env.example to .env and fill it in" >&2
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
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared is not installed (https://developers.cloudflare.com/cloudflare-one/)" >&2
  exit 1
fi

cloudflared tunnel --no-autoupdate --protocol http2 run \
  --url "http://localhost:$PORT" "$TUNNEL" > .cloudflared.log 2>&1 &
TUNNEL_PID=$!
# No `exec` below: this trap is what stops the tunnel when jarvis exits.
trap 'kill "$TUNNEL_PID" 2>/dev/null || true' EXIT

echo "tunnel:  https://$PUBLIC_HOST -> http://localhost:$PORT  (log: .cloudflared.log)"
echo "webhook: https://$PUBLIC_HOST/twilio/voice"

uv run jarvis serve --no-wakeword "$@"

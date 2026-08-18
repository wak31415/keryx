#!/usr/bin/env bash
# Dev loop for the phone channel: an ngrok tunnel on PUBLIC_HOST plus `jarvis serve`.
#
# Point the Twilio number's voice webhook at https://$PUBLIC_HOST/twilio/voice and call in.
# The wake word is off here (the tunnel, not the mic, is what we are exercising); pass
# extra flags straight through, e.g. `scripts/dev.sh --no-phone`.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "no .env found: copy .env.example to .env and fill it in" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

if [[ -z "${PUBLIC_HOST:-}" ]]; then
  echo "PUBLIC_HOST is not set in .env (use a reserved ngrok domain)" >&2
  exit 1
fi
if ! command -v ngrok >/dev/null 2>&1; then
  echo "ngrok is not installed (brew install ngrok)" >&2
  exit 1
fi

ngrok http --domain="$PUBLIC_HOST" "${PORT:-8080}" --log=stdout > .ngrok.log &
NGROK_PID=$!
# No `exec` below: this trap is what stops the tunnel when jarvis exits.
trap 'kill "$NGROK_PID" 2>/dev/null || true' EXIT

echo "tunnel:  https://$PUBLIC_HOST -> http://localhost:${PORT:-8080}  (log: .ngrok.log)"
echo "webhook: https://$PUBLIC_HOST/twilio/voice"

uv run jarvis serve --no-wakeword "$@"

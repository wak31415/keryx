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

env_value() {
  # env_value NAME [FILE] — the value of NAME in a .env file, without surrounding quotes.
  # Deliberately not `source`: .env holds JSON (PROJECTS={"a": "/b"}), and sourcing that
  # under `set -e` is a syntax error at best and arbitrary code at worst.
  local name="$1" file="${2:-.env}" value
  value="$(grep -E "^[[:space:]]*${name}=" "$file" | tail -n 1 | cut -d= -f2-)" || true
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

PUBLIC_HOST="$(env_value PUBLIC_HOST)"
PORT="$(env_value PORT)"
PORT="${PORT:-8080}"

if [[ -z "$PUBLIC_HOST" ]]; then
  echo "PUBLIC_HOST is not set in .env (use a reserved ngrok domain)" >&2
  exit 1
fi
if ! command -v ngrok >/dev/null 2>&1; then
  echo "ngrok is not installed (brew install ngrok)" >&2
  exit 1
fi

ngrok http --domain="$PUBLIC_HOST" "$PORT" --log=stdout > .ngrok.log &
NGROK_PID=$!
# No `exec` below: this trap is what stops the tunnel when jarvis exits.
trap 'kill "$NGROK_PID" 2>/dev/null || true' EXIT

echo "tunnel:  https://$PUBLIC_HOST -> http://localhost:$PORT  (log: .ngrok.log)"
echo "webhook: https://$PUBLIC_HOST/twilio/voice"

uv run jarvis serve --no-wakeword "$@"

#!/usr/bin/env bash
# Install (or remove) the launchd agents that keep Jarvis and its ngrok tunnel running.
#
#   scripts/install-launchd.sh              # render the templates and load both agents
#   scripts/install-launchd.sh --uninstall  # unload both agents and delete the plists
#
# The templates in ops/launchd/ carry __PLACEHOLDER__ names; this script fills them in
# from `command -v` and .env, writes the result to ~/Library/LaunchAgents/, and hands them
# to launchctl. Logs land in ~/.jarvis/logs/.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATES="$REPO/ops/launchd"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/.jarvis/logs"
LABELS=(com.william.jarvis com.william.ngrok)

uninstall() {
  for label in "${LABELS[@]}"; do
    launchctl bootout "gui/$UID/$label" 2>/dev/null || true
    rm -f "$AGENTS/$label.plist"
    echo "removed $label"
  done
}

if [[ "${1:-}" == "--uninstall" ]]; then
  uninstall
  exit 0
fi
if [[ -n "${1:-}" ]]; then
  echo "usage: $(basename "$0") [--uninstall]" >&2
  exit 2
fi

if [[ ! -f "$REPO/.env" ]]; then
  echo "no .env in $REPO: copy .env.example to .env and fill it in" >&2
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

PUBLIC_HOST="$(env_value PUBLIC_HOST "$REPO/.env")"
PORT="$(env_value PORT "$REPO/.env")"
PORT="${PORT:-8080}"

if [[ -z "$PUBLIC_HOST" ]]; then
  echo "PUBLIC_HOST is not set in .env (use a reserved ngrok domain)" >&2
  exit 1
fi

UV="$(command -v uv || true)"
NGROK="$(command -v ngrok || true)"
if [[ -z "$UV" ]]; then
  echo "uv is not on PATH (https://docs.astral.sh/uv/)" >&2
  exit 1
fi
if [[ -z "$NGROK" ]]; then
  echo "ngrok is not on PATH (brew install ngrok)" >&2
  exit 1
fi

mkdir -p "$AGENTS" "$LOGS"

render() {
  # render <template> <destination>
  sed -e "s|__REPO__|$REPO|g" \
      -e "s|__HOME__|$HOME|g" \
      -e "s|__UV__|$UV|g" \
      -e "s|__NGROK__|$NGROK|g" \
      -e "s|__PUBLIC_HOST__|$PUBLIC_HOST|g" \
      -e "s|__PORT__|$PORT|g" \
      "$1" > "$2"
}

for label in "${LABELS[@]}"; do
  plist="$AGENTS/$label.plist"
  render "$TEMPLATES/$label.plist" "$plist"
  # A previous version may still be loaded; booting it out first makes this re-runnable.
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID" "$plist"
  echo "loaded $label ($plist)"
done

cat <<EOF

Jarvis is running under launchd.

  status:  launchctl print gui/$UID/com.william.jarvis | head -20
  logs:    tail -f $LOGS/jarvis.err.log $HOME/.jarvis/logs/jarvis.log
  stop:    $0 --uninstall

Point the Twilio number's voice webhook at https://$PUBLIC_HOST/twilio/voice (HTTP POST).
EOF

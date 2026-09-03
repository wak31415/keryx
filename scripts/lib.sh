# Shared helpers for the scripts in this directory. Source it; do not run it.
#
# Sourcing sets REPO, ENV_FILE and LOGS, and provides the scaffolding that
# install-systemd.sh and install-launchd.sh each had their own copy of: argument parsing,
# the env-file and PATH checks, template rendering, and the closing banner. What is left in
# the installers is what is genuinely different — systemd units versus launchd agents.
#
# Deliberately no `set -euo pipefail` here: this file is sourced, and a sourced file should
# not change the shell options of whoever sourced it. Every script that needs them sets
# them itself, before the source line.

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO/.env"
LOGS="$HOME/.jarvis/logs"
#: Set by `parse_install_args` when `--uninstall` was passed.
UNINSTALL=0

env_value() {
  # env_value NAME [FILE] — the value of NAME in an env file, without surrounding quotes.
  # Deliberately not `source`: the file holds JSON (PROJECTS={"a": "/b"}), and sourcing
  # that under `set -e` is a syntax error at best and arbitrary code at worst.
  local name="$1" file="${2:-$ENV_FILE}" value
  value="$(grep -E "^[[:space:]]*${name}=" "$file" | tail -n 1 | cut -d= -f2-)" || true
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

parse_install_args() {
  # parse_install_args "$@" — sets UNINSTALL=1 for `--uninstall`; exits 2 on anything else.
  case "${1:-}" in
    "") ;;
    --uninstall) UNINSTALL=1 ;;
    *)
      echo "usage: $(basename "$0") [--uninstall]" >&2
      exit 2
      ;;
  esac
}

require_env_file() {
  # The installers read PUBLIC_HOST and friends out of it; without one there is nothing
  # to render a unit from.
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "no env file at $ENV_FILE: copy .env.example to .env and fill it in" >&2
    exit 1
  fi
}

require_command() {
  # require_command VAR NAME HINT — sets VAR to NAME's absolute path, or explains and exits.
  #
  # A variable rather than stdout on purpose: `VAR="$(...)"` would run the exit inside a
  # subshell, so a missing binary would print the hint and then carry on with an empty
  # path — which is exactly the bug this shape cannot have.
  local found
  found="$(command -v "$2" || true)"
  if [[ -z "$found" ]]; then
    echo "$2 is not on PATH ($3)" >&2
    exit 1
  fi
  printf -v "$1" '%s' "$found"
}

require_public_host() {
  # require_public_host HINT — sets PUBLIC_HOST and PORT from the env file, or exits.
  PUBLIC_HOST="$(env_value PUBLIC_HOST)"
  PORT="$(env_value PORT)"
  PORT="${PORT:-8080}"
  if [[ -z "$PUBLIC_HOST" ]]; then
    echo "PUBLIC_HOST is not set in $ENV_FILE ($1)" >&2
    exit 1
  fi
}

make_dirs() {
  # make_dirs [DIR ...] — the log directory, plus wherever this platform's units live.
  mkdir -p "$LOGS" "$@"
}

render() {
  # render TEMPLATE DEST [KEY=VALUE ...] — fill the template's __KEY__ placeholders.
  # __REPO__ and __HOME__ are always substituted; every template uses both.
  local template="$1" dest="$2"
  shift 2
  local -a args=(-e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g")
  local pair
  for pair in "$@"; do
    args+=(-e "s|__${pair%%=*}__|${pair#*=}|g")
  done
  sed "${args[@]}" "$template" > "$dest"
}

webhook_hint() {
  # The last thing both installers print: the two URLs to paste into the Twilio console.
  cat <<HINT

Point the Twilio number's voice webhook at https://$PUBLIC_HOST/twilio/voice (HTTP POST)
and the status callback at https://$PUBLIC_HOST/twilio/status.
HINT
}

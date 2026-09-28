# Shared helpers for the scripts in this directory. Source it; do not run it.
#
# Sourcing sets REPO, JARVIS_HOME_DIR, JARVIS_DIR and LOGS, and provides the scaffolding that
# install-systemd.sh and install-launchd.sh each had their own copy of: argument parsing,
# the configuration and PATH checks, template rendering, and the closing banner. What is left
# in the installers is what is genuinely different — systemd units versus launchd agents.
#
# Settings are read through `jarvis config get`, the way the service reads them — the store
# in JARVIS_HOME, a legacy .env in the repository, the defaults — and never by grepping a
# file. JARVIS_CLI says how to run jarvis (default `uv run --project "$REPO" jarvis`); the
# tests point it at the interpreter running them.
#
# Deliberately no `set -euo pipefail` here: this file is sourced, and a sourced file should
# not change the shell options of whoever sourced it. Every script that needs them sets
# them itself, before the source line.

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
#: Set by `parse_install_args` when `--uninstall` was passed.
UNINSTALL=0

config_value() {
  # config_value NAME — the value jarvis would use for NAME; empty when it has none. Run from
  # the repository, the service's working directory, so a legacy .env there still counts.
  # Never a secret: `jarvis config get` refuses those, and a config.toml that does not parse.
  local -a cli
  if [[ -n "${JARVIS_CLI:-}" ]]; then
    read -r -a cli <<< "$JARVIS_CLI"  # a command line, split on purpose
  elif command -v uv >/dev/null 2>&1; then
    cli=(uv run --quiet --project "$REPO" jarvis)  # an array: $REPO may hold a space
  else
    echo "uv is not on PATH (https://docs.astral.sh/uv/)" >&2
    return 1
  fi
  (cd "$REPO" && "${cli[@]}" config get "$1")
}

resolve_data_dir() {
  # resolve_data_dir — DATA_DIR the way the service resolves it: `~` expanded, a relative
  # path taken from the repository. Everything the service writes, its logs included, is here.
  local dir
  dir="$(config_value DATA_DIR)" || return 1
  dir="${dir:-~/.jarvis}"
  case "$dir" in
    "~") dir="$HOME" ;;
    "~/"*) dir="$HOME/${dir#\~/}" ;;
  esac
  [[ "$dir" == /* ]] || dir="$REPO/$dir"
  printf '%s' "${dir%/}"
}

#: Where the configuration lives; rendered into the units so the service reads the same one.
JARVIS_HOME_DIR="${JARVIS_HOME:-$HOME/.jarvis}"
#: The resolved DATA_DIR. Not called DATA_DIR: if the caller's shell exports one, assigning
#: it here would hand the value to every `jarvis` the script runs.
JARVIS_DIR="$(resolve_data_dir)" || JARVIS_DIR="$HOME/.jarvis"
LOGS="$JARVIS_DIR/logs"

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
  # require_public_host HINT — sets PUBLIC_HOST and PORT from the configuration, or exits.
  PUBLIC_HOST="$(config_value PUBLIC_HOST)"
  PORT="$(config_value PORT)"
  PORT="${PORT:-8080}"
  if [[ -z "$PUBLIC_HOST" ]]; then
    echo "PUBLIC_HOST is not set ($1): run \`jarvis setup\`, or" >&2
    echo "  jarvis config set PUBLIC_HOST jarvis.example.com" >&2
    exit 1
  fi
}

make_dirs() {
  # make_dirs [DIR ...] — the log directory, plus wherever this platform's units live.
  # Owner-only when it creates DATA_DIR itself, as `jarvis` would (config.secure_dir).
  (umask 077 && mkdir -p "$LOGS")
  mkdir -p "$@"
}

sed_literal() {
  # sed_literal VALUE — VALUE made literal on the replacement side of `s|…|…|`: the
  # delimiter, `&` (which means "the whole match") and the backslash that escapes both.
  printf '%s' "$1" | sed -e 's/[\\|&]/\\&/g'
}

xml_escape() {
  # xml_escape VALUE — VALUE as plist character data.
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

systemd_path() {
  # systemd_path VALUE — a path for a unit-file setting that takes one unquoted (such as
  # StandardOutput=append:…), where only `%`, the specifier character, means anything.
  printf '%s' "$1" | sed -e 's/%/%%/g'
}

systemd_quoted() {
  # systemd_quoted VALUE — VALUE for inside "…" in Environment=, which unescapes C-style
  # and then expands specifiers: a backslash or a double quote would escape or end the
  # quoting, and `%` would be a specifier.
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/%/%%/g'
}

render() {
  # render TEMPLATE DEST [KEY=VALUE ...] — fill the template's __KEY__ placeholders.
  # __REPO__ and __HOME__ are always substituted; every template uses both. Values go in
  # literally, whatever they hold (a PATH can hold `|` and `&`); escaping them for the
  # file's own syntax is the caller's business, because it differs by where they land.
  local template="$1" dest="$2"
  shift 2
  local -a args=()
  local pair
  for pair in "REPO=$REPO" "HOME=$HOME" "$@"; do
    args+=(-e "s|__${pair%%=*}__|$(sed_literal "${pair#*=}")|g")
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

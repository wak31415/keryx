# Shared helpers for the scripts in this directory. Source it; do not run it.
#
# Sourcing sets REPO, the RESOLVED_* paths (below), KERYX_HOME_DIR, KERYX_DIR and LOGS, and
# provides the scaffolding that install-systemd.sh and install-launchd.sh each had their own
# copy of: argument parsing, the configuration and PATH checks, template rendering, and the
# closing banner. What is left in the installers is what is genuinely different — systemd
# units versus launchd agents.
#
# Settings are read through `keryx config get`, and where Keryx keeps things through
# `keryx config path --shell` — the way the service reads them, from the store in
# KERYX_HOME and the defaults — and never by grepping a file or restating a default here.
# KERYX_CLI says how to run keryx (default `uv run --project "$REPO" keryx`); the tests
# point it at the interpreter running them.
#
# Deliberately no `set -euo pipefail` here: this file is sourced, and a sourced file should
# not change the shell options of whoever sourced it. Every script that needs them sets
# them itself, before the source line.

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
#: Set by `parse_install_args` when `--uninstall` was passed.
UNINSTALL=0

keryx_cli() {
  # keryx_cli ARGS... — run keryx from the repository, the service's working directory.
  local -a cli
  if [[ -n "${KERYX_CLI:-}" ]]; then
    read -r -a cli <<< "$KERYX_CLI"  # a command line, split on purpose
  elif command -v uv >/dev/null 2>&1; then
    cli=(uv run --quiet --project "$REPO" keryx)  # an array: $REPO may hold a space
  else
    echo "uv is not on PATH (https://docs.astral.sh/uv/)" >&2
    return 1
  fi
  (cd "$REPO" && "${cli[@]}" "$@")
}

config_value() {
  # config_value NAME — the value keryx would use for NAME; empty when it has none.
  # Never a secret: `keryx config get` refuses those, and a config.toml that does not parse.
  keryx_cli config get "$1"
}

resolve_paths() {
  # resolve_paths — set RESOLVED_<NAME> for every line of `keryx config path --shell`:
  # KERYX_HOME, DATA_DIR, STATE_DIR, CACHE_DIR and the four XDG_*_HOME they came from.
  # Prefixed, so that a DATA_DIR or XDG_STATE_HOME this shell exports is never reassigned
  # and handed to every `keryx` the script runs afterwards. Only NAME='value' lines are
  # taken, each quoted by keryx for exactly this eval.
  local output line
  output="$(keryx_cli config path --shell)" || return 1
  while IFS= read -r line; do
    [[ "$line" =~ ^[A-Z_]+= ]] && eval "RESOLVED_$line"
  done <<< "$output"
  [[ -n "${RESOLVED_STATE_DIR:-}" ]] || {
    echo "keryx did not say where it keeps things (keryx config path --shell)" >&2
    return 1
  }
}

resolve_paths || true
#: Where the configuration lives; rendered into the units so the service reads the same one.
KERYX_HOME_DIR="${RESOLVED_KERYX_HOME:-}"
#: The resolved DATA_DIR. Not called DATA_DIR, for the reason `resolve_paths` gives.
KERYX_DIR="${RESOLVED_DATA_DIR:-}"
#: Where the service's logs go: STATE_DIR/logs, where `keryx restart` reads them back.
LOGS="${RESOLVED_STATE_DIR:+$RESOLVED_STATE_DIR/logs}"

require_paths() {
  # require_paths — exit unless `resolve_paths` found where Keryx keeps things.
  if [[ -z "$LOGS" ]]; then
    echo "could not ask keryx where it keeps things; run \`keryx config path\` to see why" >&2
    exit 1
  fi
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
    echo "PUBLIC_HOST is not set ($1): run \`keryx setup\`, or" >&2
    echo "  keryx config set PUBLIC_HOST keryx.example.com" >&2
    exit 1
  fi
}

make_dirs() {
  # make_dirs [DIR ...] — the log directory, plus wherever this platform's units live.
  # Owner-only when it creates STATE_DIR itself, as `keryx` would (config.secure_dir).
  require_paths
  (umask 077 && mkdir -p "$LOGS")
  if (( $# )); then
    mkdir -p "$@"
  fi
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

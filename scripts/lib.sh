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
#: Set by `parse_install_args` for `--llm` / `--voice` / `--voice-llm`: the local model
#: servers' units (`keryx models serve llm|voice|voice-llm`) instead of Keryx and its tunnel.
LLM=0
VOICE=0
VOICE_LLM=0

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

account_home() {
  # account_home — the account's own home directory, from the user database (`~user`) rather
  # than $HOME: where launchd and systemd's user manager read the units, whatever this shell
  # has moved HOME to. Empty when the user database does not say.
  local user home
  user="$(id -un 2>/dev/null)" || return 0
  [[ "$user" =~ ^[A-Za-z0-9._-]+$ ]] || return 0
  home="$(eval "printf '%s' ~$user")"  # a bare word, checked above: tilde expansion only
  if [[ "$home" == /* ]]; then
    printf '%s' "$home"
  fi
}

rendered_value() {
  # rendered_value FILE NAME — NAME as a unit file was rendered with it, its quoting undone:
  # a plist's <key>NAME</key> then <string>…</string>, a systemd unit's Environment="NAME=…".
  if [[ "$1" == *.plist ]]; then
    sed -n "/<key>$2<\/key>/{n;s/.*<string>\(.*\)<\/string>.*/\1/p;q;}" "$1" \
      | sed -e 's/&lt;/</g' -e 's/&gt;/>/g' -e 's/&amp;/\&/g'
  else
    sed -n "s/^Environment=\"$2=\(.*\)\"\$/\1/p" "$1" | sed -e 's/\\\(.\)/\1/g' -e 's/%%/%/g'
  fi
}

mentions_home() {
  # mentions_home FILE — whether FILE names KERYX_HOME where a value would go: a plist's
  # <key>KERYX_HOME</key>, a systemd unit's line that is not a comment.
  if [[ "$1" == *.plist ]]; then
    grep -q '<key>KERYX_HOME</key>' "$1"
  else
    grep -v '^[[:space:]]*[#;]' "$1" | grep -q KERYX_HOME
  fi
}

expand_home() {
  # expand_home VALUE — VALUE with a leading `~` read against the account's home, as
  # `keryx.restart.service.rendered_home` reads it; as it is otherwise (and a value that is
  # still not absolute is never this home).
  local account
  if [[ "$1" == "~" || "$1" == "~/"* ]]; then
    account="$(account_home)"
    if [[ -n "$account" ]]; then
      printf '%s%s' "$account" "${1:1}"
      return 0
    fi
  fi
  printf '%s' "$1"
}

rendered_home() {
  # rendered_home FILE — the KERYX_HOME a unit file was rendered for, as
  # `keryx.restart.service.rendered_home` reads it: its KERYX_HOME; else its
  # XDG_CONFIG_HOME's keryx; else the default under the account's home — a unit from before
  # either was rendered ran with that. Fails (1) for a file that cannot be read, or that
  # names KERYX_HOME with no value to be read: whose that is cannot be told.
  local home config account
  [[ -r "$1" ]] || return 1
  home="$(rendered_value "$1" KERYX_HOME)"
  if [[ -n "${home//[[:space:]]/}" ]]; then
    expand_home "$home"
    return 0
  fi
  if mentions_home "$1"; then
    return 1
  fi
  config="$(rendered_value "$1" XDG_CONFIG_HOME)"
  if [[ "$config" == /* ]]; then
    printf '%s/keryx' "$config"
  else
    account="$(account_home)"
    printf '%s/.config/keryx' "${account:-$HOME}"
  fi
}

real_path() {
  # real_path PATH — PATH with the symlinks in as much of it as exists resolved, so two
  # spellings of one directory compare equal (os.path.realpath's answer).
  local path="${1%/}" rest=""
  while [[ -n "$path" && ! -d "$path" ]]; do
    rest="/${path##*/}$rest"
    path="${path%/*}"
  done
  if [[ -n "$path" ]]; then
    path="$(cd "$path" && pwd -P)"
  fi
  printf '%s%s' "$path" "$rest"
}

manager_view() {
  # manager_view NAME — what the account's service manager says of the unit file NAME
  # (X.plist: launchd's job X; X.service: systemd's unit), read-only, as `keryx.restart.
  # service.manager_owners` asks it: VIEW_STATE (loaded, none — nothing by that name, or no
  # such manager — or unknown, when it cannot be asked), VIEW_FILE (where it loaded it
  # from) and VIEW_HOME (the KERYX_HOME it runs it with), each empty when unsaid.
  local name="$1" out code=0 env home
  VIEW_STATE=none VIEW_FILE="" VIEW_HOME=""
  if [[ "$name" == *.plist ]]; then
    command -v launchctl >/dev/null 2>&1 || return 0
    out="$(launchctl print "gui/$UID/${name%.plist}" 2>/dev/null)" || code=$?
    if (( code == 113 )); then
      return 0  # not loaded
    elif (( code != 0 )); then
      VIEW_STATE=unknown
      return 0
    fi
    VIEW_STATE=loaded
    VIEW_FILE="$(printf '%s\n' "$out" | sed -n 's/^[[:space:]]*path = //p' | sed -n 1p)"
    VIEW_HOME="$(printf '%s\n' "$out" \
      | sed -n '/^[[:space:]]*environment = {/,/^[[:space:]]*}/s/^[[:space:]]*KERYX_HOME => //p' \
      | sed -n 1p)"
  else
    command -v systemctl >/dev/null 2>&1 || return 0
    out="$(systemctl --user show -p LoadState -p FragmentPath -p Environment "$name" \
      2>/dev/null)" || code=$?
    if (( code != 0 )); then
      VIEW_STATE=unknown
      return 0
    fi
    [[ "$(printf '%s\n' "$out" | sed -n 's/^LoadState=//p')" == loaded ]] || return 0
    VIEW_STATE=loaded
    VIEW_FILE="$(printf '%s\n' "$out" | sed -n 's/^FragmentPath=//p')"
    env="$(printf '%s\n' "$out" | sed -n 's/^Environment=//p')"
    # "KERYX_HOME=…" quoted (its \" and \\ undone), else KERYX_HOME=… up to a space.
    home="$(printf '%s\n' "$env" | sed -nE 's/^(.* )?"KERYX_HOME=(([^"\\]|\\.)*)".*$/\2/p' \
      | sed -e 's/\\\(.\)/\1/g')"
    if [[ -z "$home" ]]; then
      home="$(printf '%s\n' "$env" | sed -nE 's/^(.* )?KERYX_HOME=([^ ]*).*$/\2/p')"
    fi
    VIEW_HOME="$home"
    if [[ -z "$home" ]] && printf '%s' "$env" | grep -q KERYX_HOME; then
      VIEW_STATE=unknown  # named, with no value to be read
    fi
  fi
}

refuse_foreign() {
  # refuse_foreign THEIRS WHERE — exit 2 unless THEIRS (the KERYX_HOME a unit was rendered
  # for, read at WHERE) is this one; a THEIRS that is not absolute is never this one.
  require_home_dir
  if [[ "$1" != /* || "$(real_path "$1")" != "$(real_path "$KERYX_HOME_DIR")" ]]; then
    echo "These units belong to KERYX_HOME=$1; this command runs with KERYX_HOME=$KERYX_HOME_DIR." >&2
    echo "rendered in $2" >&2
    exit 2
  fi
}

refuse_unreadable() {
  # refuse_unreadable WHERE — exit 2: whose the units are cannot be told from WHERE, and
  # what cannot be told is refused.
  require_home_dir
  echo "Could not tell which KERYX_HOME these units belong to; this command runs with KERYX_HOME=$KERYX_HOME_DIR." >&2
  echo "could not read $1" >&2
  exit 2
}

require_home_dir() {
  if [[ -z "$KERYX_HOME_DIR" ]]; then
    echo "could not ask keryx where it keeps things; run \`keryx config path\` to see why" >&2
    exit 1
  fi
}

judge_file() {
  # judge_file FILE — refuse_foreign (or refuse_unreadable) for one unit file.
  local theirs
  if ! theirs="$(rendered_home "$1")"; then
    refuse_unreadable "$1"
  fi
  refuse_foreign "$theirs" "$1"
}

require_own_units() {
  # require_own_units DIR SUBDIR AGENT TUNNEL NAME... — exit 2, saying whose they are, when
  # a unit NAME (dev.keryx.llm.plist, keryx.service) runs with, or was rendered for, another
  # KERYX_HOME than this one — or whose it is cannot be told — before anything is touched: a
  # unit's name is the account's, not the home's, so installing or removing it from another
  # KERYX_HOME — a sandbox, a test — would boot out and replace the owner's own. The service
  # manager is asked first what it has loaded by that name (`manager_view`), wherever its
  # file is; then each file is read in DIR, where this installer writes it, and in SUBDIR of
  # the account's own home (`account_home`), where the manager reads it whatever HOME says.
  # TUNNEL renders no Keryx environment and is judged by AGENT, Keryx's own unit, when that
  # is installed. `keryx.restart.service.foreign_units` is the same check, for the commands
  # that drive the manager themselves; there is no override.
  local dir="$1" subdir="$2" agent="$3" tunnel="$4" home name judged where file answer
  shift 4
  local -a dirs=("$dir")
  home="$(account_home)"
  if [[ -n "$home" && "$home/$subdir" != "$dir" ]]; then
    dirs+=("$home/$subdir")
  fi
  for name in "$@"; do
    judged="$name"
    if [[ "$name" == "$tunnel" ]]; then
      manager_view "$agent"
      if [[ "$VIEW_STATE" != none ]]; then
        judged="$agent"
      fi
      for where in "${dirs[@]}"; do
        if [[ -e "$where/$agent" ]]; then
          judged="$agent"
        fi
      done
    fi
    manager_view "$judged"
    answer="the service manager's answer about $judged"
    case "$VIEW_STATE" in
      unknown) refuse_unreadable "$answer" ;;
      loaded)
        if [[ -n "$VIEW_HOME" ]]; then
          refuse_foreign "$(expand_home "$VIEW_HOME")" "${VIEW_FILE:-$answer}"
        elif [[ -n "$VIEW_FILE" ]]; then
          judge_file "$VIEW_FILE"  # gone, or unreadable: refused
        else
          refuse_unreadable "$answer"
        fi
        ;;
    esac
    for where in "${dirs[@]}"; do
      if [[ -d "$where" && ! -x "$where" ]]; then
        refuse_unreadable "$where"
      fi
      file="$where/$judged"
      if [[ -e "$file" || -L "$file" ]]; then
        judge_file "$file"
      fi
    done
  done
}

parse_install_args() {
  # parse_install_args "$@" — sets UNINSTALL=1 for `--uninstall`, LLM=1 for `--llm`,
  # VOICE=1 for `--voice` and VOICE_LLM=1 for `--voice-llm`, in any order; exits 2 on
  # anything else.
  local arg
  for arg in "$@"; do
    case "$arg" in
      --uninstall) UNINSTALL=1 ;;
      --llm) LLM=1 ;;
      --voice) VOICE=1 ;;
      --voice-llm) VOICE_LLM=1 ;;
      *)
        echo "usage: $(basename "$0") [--llm] [--voice] [--voice-llm] [--uninstall]" >&2
        exit 2
        ;;
    esac
  done
}

require_own_voice_llm_port() {
  # require_own_voice_llm_port — exit 1, saying so, when VOICE_LLM_SERVER_PORT is the port of
  # the agent's model server or the voice server: two servers on one port both fail to bind
  # and restart for ever. Nothing is installed first. (`servers.port_clash` says it the same.)
  local port others=()
  port="$(config_value VOICE_LLM_SERVER_PORT)" || exit 1
  [[ "$port" == "$(config_value AGENT_SERVER_PORT)" ]] && others+=("the agent's model server")
  [[ "$port" == "$(config_value VOICE_SERVER_PORT)" ]] && others+=("the voice server")
  (( ${#others[@]} )) || return 0
  local names="${others[0]}"
  (( ${#others[@]} > 1 )) && names="$names and ${others[1]}"
  echo "The voice LLM's server and $names are both set to port $port: give the voice LLM a port of its own." >&2
  exit 1
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

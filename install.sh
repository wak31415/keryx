#!/bin/sh
# Install Keryx:
#
#   curl -fsSL https://raw.githubusercontent.com/wak31415/keryx/main/install.sh | sh
#
# Installs the `keryx` command with both coding agents, then runs `keryx setup`, which asks
# only for what is missing and ends by installing the background service. Run it again to
# repair an install; `keryx upgrade` updates one. Nothing here needs root.
#
# Keryx is the Python package `keryx-voice`, installed by uv into an environment of its own,
# with a Python of its own. A machine without uv gets one in ~/.local/share/keryx/bin, kept
# off PATH: the only command anyone needs is `keryx`.
#
# KERYX_PACKAGE installs something else in its place: a version (`keryx-voice[all]==0.2.0`)
# or a checkout (`keryx-voice[all] @ git+https://github.com/wak31415/keryx`).
set -eu

package="${KERYX_PACKAGE:-keryx-voice[all]}"

fail() {
  printf 'keryx install: %s\n' "$*" >&2
  exit 1
}

case "$(uname -s)" in
  Linux | Darwin) ;;
  *) fail "Keryx runs on Linux and macOS." ;;
esac

# The same directory `keryx upgrade` looks in (keryx.install.private_uv): XDG_DATA_HOME
# only when it is absolute, as the XDG specification says.
case "${XDG_DATA_HOME:-}" in
  /*) data="$XDG_DATA_HOME" ;;
  *) data="$HOME/.local/share" ;;
esac
private="$data/keryx/bin"

if [ -x "$private/uv" ]; then
  uv="$private/uv"
elif command -v uv >/dev/null 2>&1; then
  uv="$(command -v uv)"
else
  echo "Fetching the installer Keryx uses…"
  (umask 077 && mkdir -p "$data/keryx")
  curl -LsSf https://astral.sh/uv/install.sh \
    | env UV_INSTALL_DIR="$private" UV_NO_MODIFY_PATH=1 sh -s -- --quiet
  uv="$private/uv"
  [ -x "$uv" ] || fail "could not fetch uv into $private"
fi

echo "Installing Keryx (a few hundred MB: both coding agents come with it)…"
"$uv" tool install --quiet --upgrade --python 3.12 "$package"

bin="$("$uv" tool dir --bin)"
case ":$PATH:" in
  *":$bin:"*) on_path=1 ;;
  *)
    on_path=0
    "$uv" tool update-shell --quiet >/dev/null 2>&1 || true
    PATH="$bin:$PATH"
    export PATH
    ;;
esac
echo "Installed $("$bin/keryx" --version)."

# `curl … | sh` hands this script's stdin to the pipe, so the wizard reads the terminal.
if [ -t 1 ] && (exec </dev/tty) 2>/dev/null; then
  echo
  keryx setup </dev/tty || echo "Run \`keryx setup\` again whenever you are ready."
else
  echo "Next: keryx setup"
fi

if [ "$on_path" = 0 ]; then
  echo
  echo "Open a new terminal to use the keryx command (it is in $bin)."
fi

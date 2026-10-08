#!/usr/bin/env bash
# Run the scripts' tests (test-scripts.sh: build-app.sh and make-signing-identity.sh, in a
# temporary folder), then the Swift tests. With only the Command Line Tools installed (no
# Xcode), Swift Testing lives in a framework directory swift does not search by default;
# add it then, and only then.
set -euo pipefail
cd "$(dirname "$0")/.."
scripts/test-scripts.sh
flags=()
if [[ "$(xcode-select -p 2>/dev/null)" == */CommandLineTools ]]; then
  fw=/Library/Developer/CommandLineTools/Library/Developer/Frameworks
  # The Command Line Tools package ships Testing.framework but not a usable
  # _Testing_Foundation overlay (no .swiftmodule in the framework), so importing
  # Testing and Foundation in the same module triggers a cross-import the
  # compiler cannot satisfy. Disable cross-import overlay search entirely.
  flags=(-Xswiftc "-F$fw" -Xlinker "-F$fw" -Xlinker -rpath -Xlinker "$fw" \
         -Xswiftc -Xfrontend -Xswiftc -disable-cross-import-overlays)
fi
# `${flags[@]+…}`: bash 3.2 (macOS's) treats an empty array as unset under `set -u`.
exec swift test ${flags[@]+"${flags[@]}"} "$@"

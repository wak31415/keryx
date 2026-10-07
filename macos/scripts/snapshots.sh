#!/usr/bin/env bash
# Build KeryxSnapshots and photograph every catalog screen into macos/build/snapshots/
# (light and dark, plus index.html). Pass --offscreen to skip real windows.
set -euo pipefail
cd "$(dirname "$0")/.."
swift build --product KeryxSnapshots
out=build/snapshots
mkdir -p "$out"
exec "$(swift build --show-bin-path)/KeryxSnapshots" "$out" "$@"

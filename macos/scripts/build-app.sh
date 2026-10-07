#!/usr/bin/env bash
# Build Keryx.app into macos/build/: a release build of KeryxApp, wrapped in a bundle by
# hand (no Xcode needed) and signed ad hoc. Pass --install to copy it to /Applications,
# replacing any copy already there.
#
# The icon is one of two committed files, chosen for the macOS this builds on: on 26 and
# later, Resources/AppIcon.icns (full bleed; the system masks it into its own shape);
# before, Resources/AppIcon-legacy.icns (a tile already shaped). Either way it ships as
# Contents/Resources/AppIcon.icns. With the file missing it warns and builds without one.
set -euo pipefail

# The icon for macOS `major` from the icons in `resources`, or nothing (and a warning on
# stderr) when that file is missing.
choose_icon() {
  local resources=$1 major=$2 name
  if ((major >= 26)); then name=AppIcon.icns; else name=AppIcon-legacy.icns; fi
  if [[ -f "$resources/$name" ]]; then
    echo "$resources/$name"
  else
    echo "warning: $resources/$name is missing; building without an icon" >&2
  fi
}

main() {
  local install=false
  for arg in "$@"; do
    case $arg in
      --install) install=true ;;
      -h | --help)
        echo "usage: $0 [--install]"
        return 0
        ;;
      *)
        echo "unknown argument: $arg (usage: $0 [--install])" >&2
        return 2
        ;;
    esac
  done

  cd "$(dirname "$0")/.."
  local version build
  version=$(sed -n 's/^version = "\(.*\)"$/\1/p' ../pyproject.toml | head -n 1)
  build=$(git rev-list --count HEAD)
  if [[ -z $version ]]; then
    echo "error: no version in ../pyproject.toml" >&2
    return 1
  fi

  swift build -c release --product KeryxApp
  local bin
  bin=$(swift build -c release --show-bin-path)

  local app=build/Keryx.app
  rm -rf "$app"
  mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
  cp "$bin/KeryxApp" "$app/Contents/MacOS/Keryx"
  sed -e "s/__VERSION__/$version/" -e "s/__BUILD__/$build/" Resources/Info.plist \
    >"$app/Contents/Info.plist"

  local icon
  icon=$(choose_icon Resources "$(sw_vers -productVersion | cut -d. -f1)")
  if [[ -n $icon ]]; then
    cp "$icon" "$app/Contents/Resources/AppIcon.icns"
  fi

  plutil -lint "$app/Contents/Info.plist"
  codesign --force --sign - --identifier dev.keryx.app "$app"
  codesign --verify --strict "$app"

  echo "Built $PWD/$app (version $version, build $build)"
  if $install; then
    rm -rf /Applications/Keryx.app
    cp -R "$app" /Applications/
    echo "Installed /Applications/Keryx.app"
  else
    echo "cp -R $PWD/$app /Applications/ to install"
  fi
}

# Sourced (to test choose_icon), it only defines the functions.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

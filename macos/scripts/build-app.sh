#!/usr/bin/env bash
# Build Keryx.app and put it in /Applications: a release build of KeryxApp, wrapped in a
# bundle by hand (no Xcode needed) in macos/build/, signed, then copied over the copy in
# /Applications. --no-install stops at macos/build/Keryx.app.
#
# Signing: with "Keryx Local Signing" when that identity is in the login keychain and
# trusted for code signing (scripts/make-signing-identity.sh makes it, once), so every build
# has the same designated requirement and the login item and its approval survive a
# rebuild. Without it, ad hoc, with a warning.
#
# Installing touches /Applications/Keryx.app and nothing else there: the new bundle is
# copied beside it into a temporary folder (mktemp) and checked, the old one renamed aside,
# the new one renamed into place, and only then the old one deleted. A failed rename puts the old
# one back. An app of that name that is not Keryx (another bundle identifier) is refused.
#
# The icon is one of two committed files, chosen for the macOS this builds on: on 26 and
# later, Resources/AppIcon.icns (full bleed; the system masks it into its own shape);
# before, Resources/AppIcon-legacy.icns (a tile already shaped). Either way it ships as
# Contents/Resources/AppIcon.icns. With the file missing it warns and builds without one.
#
# For tests: KERYX_APP_DESTINATION replaces /Applications, and KEYCHAIN the login keychain.
set -euo pipefail

BUNDLE_ID=dev.keryx.app
SIGNING_IDENTITY="Keryx Local Signing"

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

# The keychain to look for the identity in: KEYCHAIN, else the login keychain.
signing_keychain() {
  if [[ -n ${KEYCHAIN:-} ]]; then
    echo "$KEYCHAIN"
  else
    # Nothing over ssh, where there is no login keychain to name: then ad hoc.
    { security login-keychain 2>/dev/null || true; } | sed -e 's/^[[:space:]]*"//' -e 's/"[[:space:]]*$//'
  fi
}

# What to pass `codesign --sign`: the SHA-1 of "Keryx Local Signing" when `keychain` holds
# it and codesign may use it, else `-` (ad hoc) and a warning on stderr that says why.
choose_identity() {
  local keychain=$1 hash
  if [[ -z $keychain ]]; then
    echo "warning: no login keychain (over ssh?); signing ad hoc." >&2
    echo -
    return 0
  fi
  hash=$(security find-identity -v -p codesigning "$keychain" 2>/dev/null |
    awk -v name="\"$SIGNING_IDENTITY\"" 'index($0, name) { print $2; exit }')
  if [[ -n $hash ]]; then
    echo "$hash"
    return 0
  fi
  if security find-identity -p codesigning "$keychain" 2>/dev/null | grep -qF "\"$SIGNING_IDENTITY\""; then
    echo "warning: \"$SIGNING_IDENTITY\" is in $keychain but not trusted for code signing;" \
      "signing ad hoc. scripts/make-signing-identity.sh trusts it." >&2
  else
    echo "warning: no \"$SIGNING_IDENTITY\" in $keychain; signing ad hoc, so Open at Login" \
      "must be turned off and on again after each build. scripts/make-signing-identity.sh" \
      "makes the identity, once." >&2
  fi
  echo -
}

# The bundle identifier of the app at `path`, or nothing.
bundle_id() {
  /usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$1/Contents/Info.plist" 2>/dev/null || true
}

# Puts the bundle `app` at `destination`/Keryx.app, replacing the one there, and touches
# nothing else in `destination` but a temporary folder of its own beside it (mktemp's), which
# holds the new copy until it is renamed into place and the old one until it is deleted.
install_app() {
  local app=$1 destination=${2%/}
  local target="$destination/Keryx.app"
  if [[ ! -d $destination ]]; then
    echo "error: $destination is not a folder" >&2
    return 1
  fi
  if [[ -e $target || -L $target ]]; then
    local found
    found=$(bundle_id "$target")
    if [[ $found != "$BUNDLE_ID" ]]; then
      echo "error: $target is not Keryx (its bundle identifier is ${found:-missing}); not replacing it" >&2
      return 1
    fi
  fi

  local work
  work=$(mktemp -d "$destination/.Keryx.app.XXXXXX")
  local staged="$work/Keryx.app" old="$work/previous.app"
  if ! ditto "$app" "$staged" || ! codesign --verify --strict "$staged"; then
    rm -rf "$work"
    echo "error: the copy in $destination did not verify; $target is unchanged" >&2
    return 1
  fi
  if [[ -e $target || -L $target ]] && ! mv "$target" "$old"; then
    rm -rf "$work"
    echo "error: could not move the old Keryx.app aside; it is unchanged" >&2
    return 1
  fi
  if ! mv "$staged" "$target"; then
    if [[ -e $old || -L $old ]]; then mv "$old" "$target"; fi
    rm -rf "$work"
    echo "error: could not move the new Keryx.app into $destination; the old one is back" >&2
    return 1
  fi
  rm -rf "$work"
}

usage() {
  echo "usage: $0 [--no-install]"
}

main() {
  local install=true
  for arg in "$@"; do
    case $arg in
      --no-install) install=false ;;
      --install) install=true ;; # the default now; accepted for old habits
      -h | --help)
        usage
        return 0
        ;;
      *)
        echo "unknown argument: $arg ($(usage))" >&2
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
  local keychain identity
  keychain=$(signing_keychain)
  identity=$(choose_identity "$keychain")
  if [[ $identity == - ]]; then
    codesign --force --sign - --identifier "$BUNDLE_ID" "$app"
  else
    codesign --force --sign "$identity" --keychain "$keychain" --identifier "$BUNDLE_ID" "$app"
  fi
  codesign --verify --strict "$app"
  local signed="ad hoc"
  [[ $identity == - ]] || signed="with \"$SIGNING_IDENTITY\""

  echo "Built $PWD/$app (version $version, build $build), signed $signed"
  if ! $install; then
    return 0
  fi
  local destination=${KERYX_APP_DESTINATION:-/Applications}
  install_app "$app" "$destination"
  echo "Installed $destination/Keryx.app"
  if pgrep -qf "^$destination/Keryx.app/Contents/MacOS/Keryx" 2>/dev/null; then
    echo "Keryx is running the previous build: quit it and open it again to run this one."
  fi
}

# Sourced (to test the functions), it only defines them.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

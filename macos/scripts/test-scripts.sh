#!/usr/bin/env bash
# Tests for build-app.sh and make-signing-identity.sh, all in a temporary folder: which
# identity a build signs with, how it replaces Keryx.app (KERYX_APP_DESTINATION standing in
# for /Applications), and the identity script against a keychain of its own (KEYCHAIN). It
# never builds the app, never touches /Applications, and never touches your keychains: the
# keychain it makes is deleted, and the trust step is skipped (KERYX_SIGNING_TRUST=0).
#
#   macos/scripts/test-scripts.sh     # also run by test.sh, before the Swift tests
set -euo pipefail

SCRIPTS=$(cd "$(dirname "$0")" && pwd)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/keryx-script-tests.XXXXXX")
# Every build-app.sh this runs installs, if it ever got that far, into a folder of the test's
# own: never /Applications, whatever an argument-parsing bug would let through.
export KERYX_APP_DESTINATION="$WORK/never"
# The identity script's temporary folders, too, so the check that none is left behind looks
# only at this run's, never the shared temporary directory.
SIGNING_TMP="$WORK/tmp"
mkdir -p "$SIGNING_TMP"
KEYCHAINS=()
cleanup() {
  for chain in "${KEYCHAINS[@]+"${KEYCHAINS[@]}"}"; do
    security delete-keychain "$chain" 2>/dev/null || true
  done
  rm -rf "$WORK"
}
trap cleanup EXIT

PASSED=0
FAILED=0
fail() {
  echo "  FAIL: $*" >&2
  FAILED=$((FAILED + 1))
}
pass() {
  PASSED=$((PASSED + 1))
}
check() { # check DESCRIPTION COMMAND...
  local what=$1
  shift
  if "$@"; then pass; else fail "$what"; fi
}

# shellcheck source=macos/scripts/build-app.sh
source "$SCRIPTS/build-app.sh"
set +e # each check says what failed; one failure does not stop the rest

# --- choose_identity, against a stub `security` --------------------------------------

STUB="$WORK/stub"
mkdir -p "$STUB"
# The stub prints $ALL for `find-identity -p codesigning` and $VALID with -v, as the real
# one does: a header, numbered identities, and a count.
cat >"$STUB/security" <<'EOF'
#!/usr/bin/env bash
if [[ " $* " == *" -v "* ]]; then printf '%b' "${VALID:-}"; else printf '%b' "${ALL:-}"; fi
EOF
chmod +x "$STUB/security"

trusted='  1) 1111111111111111111111111111111111111111 "Keryx Local Signing"\n     1 valid identities found\n'
untrusted='  1) 2222222222222222222222222222222222222222 "Keryx Local Signing" (CSSMERR_TP_NOT_TRUSTED)\n'
other='  1) 3333333333333333333333333333333333333333 "Keryx Local Signing Old"\n  2) 4444444444444444444444444444444444444444 "Alacritty"\n'

identity() { # identity ALL VALID: what choose_identity prints, its warning in $WORK/err
  ALL=$1 VALID=$2 PATH="$STUB:$PATH" choose_identity "$WORK/any.keychain-db" 2>"$WORK/err"
}

echo "choose_identity"
check "a trusted identity is chosen by its hash" \
  test "$(identity "$trusted" "$trusted")" = 1111111111111111111111111111111111111111
check "a trusted identity is chosen without a warning" test ! -s "$WORK/err"
check "an untrusted identity signs ad hoc" test "$(identity "$untrusted" "")" = -
check "an untrusted identity says it is not trusted" grep -q "not trusted" "$WORK/err"
check "no identity signs ad hoc" test "$(identity "" "")" = -
check "no identity names the script that makes one" grep -q "make-signing-identity.sh" "$WORK/err"
check "no login keychain (over ssh) signs ad hoc" \
  test "$(PATH="$STUB:$PATH" choose_identity "" 2>"$WORK/err")" = -
check "no login keychain says so" grep -q "no login keychain" "$WORK/err"
check "another identity, even one with a longer name, is never chosen" \
  test "$(identity "$other" "$other")" = -

# --- install_app, into a stand-in for /Applications ----------------------------------

# A bundle with `id`, signed ad hoc, at `path`; with `marker`, that file in its Resources.
make_bundle() {
  local path=$1 id=$2 marker=${3:-}
  mkdir -p "$path/Contents/MacOS" "$path/Contents/Resources"
  cp /usr/bin/true "$path/Contents/MacOS/Keryx"
  if [[ -n $marker ]]; then echo "$marker" >"$path/Contents/Resources/$marker"; fi
  cat >"$path/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>Keryx</string>
<key>CFBundleIdentifier</key><string>$id</string>
</dict></plist>
EOF
  codesign --force --sign - --identifier "$id" "$path" 2>/dev/null
}

# The names in `folder`, hidden ones too, sorted bytewise and joined by spaces.
listing() { ls -A "$1" | LC_ALL=C sort | tr '\n' ' '; }

echo "install_app"
DEST="$WORK/Applications"
mkdir -p "$DEST"
make_bundle "$WORK/new/Keryx.app" dev.keryx.app new-marker
make_bundle "$DEST/Other.app" com.example.other
echo "keep me" >"$DEST/notes.txt"
others_before=$(cd "$DEST" && find Other.app notes.txt -type f -exec shasum {} + | LC_ALL=C sort)

install_app "$WORK/new/Keryx.app" "$DEST" 2>"$WORK/err"
check "a first install succeeds" test $? -eq 0
check "a first install puts Keryx.app in place" test -x "$DEST/Keryx.app/Contents/MacOS/Keryx"
check "the installed copy verifies" codesign --verify --strict "$DEST/Keryx.app"
check "a first install leaves no temporary names" test "$(listing "$DEST")" = "Keryx.app Other.app notes.txt "

# Replacing: the old one's own file goes with it, and nothing else in the folder changes.
rm -rf "$DEST/Keryx.app"
make_bundle "$DEST/Keryx.app" dev.keryx.app old-marker
install_app "$WORK/new/Keryx.app" "$DEST" 2>"$WORK/err"
check "a replacement succeeds" test $? -eq 0
check "a replacement removes the old bundle" test ! -e "$DEST/Keryx.app/Contents/Resources/old-marker"
check "a replacement installs the new bundle" test -e "$DEST/Keryx.app/Contents/Resources/new-marker"
check "a replacement leaves no temporary names" test "$(listing "$DEST")" = "Keryx.app Other.app notes.txt "
others_after=$(cd "$DEST" && find Other.app notes.txt -type f -exec shasum {} + | LC_ALL=C sort)
check "nothing else in the folder is touched" test "$others_before" = "$others_after"

# A copy that does not verify never replaces the one there.
make_bundle "$WORK/broken/Keryx.app" dev.keryx.app
echo tampered >>"$WORK/broken/Keryx.app/Contents/MacOS/Keryx"
install_app "$WORK/broken/Keryx.app" "$DEST" 2>"$WORK/err"
check "an unverifiable copy is refused" test $? -ne 0
check "an unverifiable copy leaves the installed one" test -e "$DEST/Keryx.app/Contents/Resources/new-marker"
check "an unverifiable copy leaves no temporary names" test "$(listing "$DEST")" = "Keryx.app Other.app notes.txt "

# A Keryx.app that is some other app is never replaced.
FOREIGN="$WORK/Foreign"
mkdir -p "$FOREIGN"
make_bundle "$FOREIGN/Keryx.app" com.example.keryx
install_app "$WORK/new/Keryx.app" "$FOREIGN" 2>"$WORK/err"
check "another app called Keryx.app is refused" test $? -ne 0
check "the refusal names its bundle identifier" grep -q "com.example.keryx" "$WORK/err"
check "another app called Keryx.app is left alone" \
  test "$(bundle_id "$FOREIGN/Keryx.app")" = com.example.keryx
check "the refusal leaves no temporary names" test "$(listing "$FOREIGN")" = "Keryx.app "

install_app "$WORK/new/Keryx.app" "$WORK/missing" 2>"$WORK/err"
check "a destination that is not a folder is refused" test $? -ne 0
check "a destination that is not a folder is not made" test ! -e "$WORK/missing"

# --- the command line, before anything is built ----------------------------------------

echo "build-app.sh arguments"
check "every build-app.sh here installs into the test's own folder" \
  test "${KERYX_APP_DESTINATION:-}" = "$WORK/never"
"$SCRIPTS/build-app.sh" --bogus >/dev/null 2>&1
check "an unknown argument exits 2 before building" test $? -eq 2
check "--help names --no-install" grep -q -- "--no-install" <("$SCRIPTS/build-app.sh" --help)

# --- make-signing-identity.sh, against a keychain of its own ---------------------------

echo "make-signing-identity.sh"
CHAIN="$WORK/test.keychain-db"
searchlist_before=$(security list-keychains -d user)
security create-keychain -p test "$CHAIN"
KEYCHAINS+=("$CHAIN")
security unlock-keychain -p test "$CHAIN"

# Answering no with the trust step on: it says what the trust is, and changes nothing.
echo n | TMPDIR="$SIGNING_TMP" KEYCHAIN="$CHAIN" "$SCRIPTS/make-signing-identity.sh" >"$WORK/out" 2>&1
check "answering no exits 1" test $? -eq 1
check "answering no adds nothing" sh -c "! security find-identity -p codesigning '$CHAIN' | grep -q 'Keryx Local Signing'"
check "the question says the trust is for every keychain" grep -q "every keychain" "$WORK/out"
check "the question says how to undo it" grep -q -- "--remove" "$WORK/out"
check "the question gives the commands by hand" grep -q "security delete-identity -Z" "$WORK/out"

# A certificate whose name contains the identity's, there first: never the one chosen.
/usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=Keryx Local Signing Old" \
  -keyout "$WORK/decoy.key" -out "$WORK/decoy.pem" 2>/dev/null
security import "$WORK/decoy.pem" -k "$CHAIN" >/dev/null

TMPDIR="$SIGNING_TMP" KEYCHAIN="$CHAIN" KERYX_SIGNING_TRUST=0 "$SCRIPTS/make-signing-identity.sh" --yes >"$WORK/out" 2>&1
check "--yes adds it without asking" test $? -eq 0
check "--yes adds a code-signing identity of that name" \
  sh -c "security find-identity -p codesigning '$CHAIN' | grep -q '\"Keryx Local Signing\"'"
# shellcheck source=macos/scripts/make-signing-identity.sh
hash=$(source "$SCRIPTS/make-signing-identity.sh" && identity_hash "$CHAIN" "Keryx Local Signing")
(source "$SCRIPTS/make-signing-identity.sh" && certificate_pem "$CHAIN" "$hash") >"$WORK/cert.pem"
check "the certificate is chosen by its SHA-1, not the one whose name contains it" \
  test "$(/usr/bin/openssl x509 -in "$WORK/cert.pem" -noout -fingerprint -sha1 | sed 's/.*=//; s/://g')" = "$hash"
check "its subject is exactly the identity's" \
  grep -q "CN *= *Keryx Local Signing$" <(/usr/bin/openssl x509 -in "$WORK/cert.pem" -noout -subject)
check "its certificate is for code signing" \
  sh -c "/usr/bin/openssl x509 -in '$WORK/cert.pem' -noout -text | grep -A1 'Extended Key Usage' | grep -q 'Code Signing'"
check "its certificate is self-signed" \
  test "$(/usr/bin/openssl x509 -in "$WORK/cert.pem" -noout -subject | sed 's/^subject= *//')" = \
  "$(/usr/bin/openssl x509 -in "$WORK/cert.pem" -noout -issuer | sed 's/^issuer= *//')"

TMPDIR="$SIGNING_TMP" KEYCHAIN="$CHAIN" KERYX_SIGNING_TRUST=0 "$SCRIPTS/make-signing-identity.sh" --yes >"$WORK/out" 2>&1
check "running it again succeeds" test $? -eq 0
check "running it again says there is nothing to do" grep -q "nothing to do" "$WORK/out"
check "running it again adds no second identity" \
  test "$(security find-identity -p codesigning "$CHAIN" | grep -c '"Keryx Local Signing"')" -eq 1
check "your keychain search list is unchanged" test "$(security list-keychains -d user)" = "$searchlist_before"
check "no temporary key is left behind" sh -c "! ls -d '$SIGNING_TMP'/keryx-signing.* >/dev/null 2>&1"

# Untrusted, so a build would sign ad hoc and say why.
check "a build would not sign with an untrusted identity" test "$(choose_identity "$CHAIN" 2>"$WORK/err")" = -
check "and says it is not trusted" grep -q "not trusted" "$WORK/err"

# --remove: asks, then takes the identity away; the decoy stays.
echo n | TMPDIR="$SIGNING_TMP" KEYCHAIN="$CHAIN" KERYX_SIGNING_TRUST=0 "$SCRIPTS/make-signing-identity.sh" --remove >"$WORK/out" 2>&1
check "--remove answered no exits 1" test $? -eq 1
check "--remove answered no keeps it" \
  sh -c "security find-identity -p codesigning '$CHAIN' | grep -q '\"Keryx Local Signing\"'"
TMPDIR="$SIGNING_TMP" KEYCHAIN="$CHAIN" KERYX_SIGNING_TRUST=0 "$SCRIPTS/make-signing-identity.sh" --remove --yes >"$WORK/out" 2>&1
check "--remove --yes succeeds" test $? -eq 0
check "--remove --yes takes the identity away" \
  sh -c "! security find-identity -p codesigning '$CHAIN' | grep -q '\"Keryx Local Signing\"'"
check "--remove leaves a certificate of another name" \
  sh -c "security find-certificate -c 'Keryx Local Signing Old' '$CHAIN' >/dev/null"
TMPDIR="$SIGNING_TMP" KEYCHAIN="$CHAIN" KERYX_SIGNING_TRUST=0 "$SCRIPTS/make-signing-identity.sh" --remove --yes >"$WORK/out" 2>&1
check "--remove with nothing there says so" grep -q "nothing to do" "$WORK/out"

echo "$PASSED passed, $FAILED failed"
((FAILED == 0))

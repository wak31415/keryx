#!/usr/bin/env bash
# Make "Keryx Local Signing", the self-signed code-signing identity build-app.sh signs with,
# in the login keychain. Once per Mac; running it again changes nothing.
#
#   macos/scripts/make-signing-identity.sh          # asks before it touches the keychain
#   macos/scripts/make-signing-identity.sh --yes    # does not ask
#
# Why: an ad-hoc signature changes with every build, and macOS ties the login item and its
# background-item approval to the signature, so each rebuild needed them turned off and on
# again. A certificate of your own gives every build the same designated requirement.
#
# What it does: a 2048-bit RSA key and a ten-year certificate for it, with the code-signing
# extended key usage, made by /usr/bin/openssl in a private temporary folder; both imported
# into the keychain, the key usable by /usr/bin/codesign; then the certificate trusted for
# code signing only (`security add-trusted-cert -p codeSign`), which macOS asks your password
# for, since codesign uses only a trusted identity. The temporary files are deleted.
#
# KEYCHAIN=/path/to.keychain-db puts it in that keychain instead of the login keychain, and
# KERYX_SIGNING_TRUST=0 skips the trust step, which changes your user trust settings
# whichever keychain holds the certificate: both are for tests, which never touch yours.
set -euo pipefail

IDENTITY="Keryx Local Signing"

# The login keychain, or KEYCHAIN.
keychain() {
  if [[ -n ${KEYCHAIN:-} ]]; then
    echo "$KEYCHAIN"
  else
    security login-keychain | sed -e 's/^[[:space:]]*"//' -e 's/"[[:space:]]*$//'
  fi
}

# Whether `name` is a code-signing identity in `keychain`: "valid" (codesign will use it),
# "untrusted" (there, but not trusted for code signing), or "missing".
identity_state() {
  local keychain=$1 name=$2 all
  all=$(security find-identity -p codesigning "$keychain" 2>/dev/null || true)
  if ! grep -q "\"$name\"" <<<"$all"; then
    echo missing
  elif security find-identity -v -p codesigning "$keychain" 2>/dev/null | grep -q "\"$name\""; then
    echo valid
  else
    echo untrusted
  fi
}

# Writes key.pem, cert.pem and identity.p12 (passphrase in `pass`) into `dir`.
make_certificate() {
  local dir=$1 pass=$2
  cat >"$dir/cert.cnf" <<EOF
[ req ]
distinguished_name = dn
x509_extensions = ext
prompt = no
[ dn ]
CN = $IDENTITY
[ ext ]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
subjectKeyIdentifier = hash
EOF
  # LibreSSL's, not a Homebrew OpenSSL 3: its PKCS#12 defaults are the ones `security
  # import` reads.
  /usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -config "$dir/cert.cnf" \
    -keyout "$dir/key.pem" -out "$dir/cert.pem" 2>/dev/null
  PASS=$pass /usr/bin/openssl pkcs12 -export -inkey "$dir/key.pem" -in "$dir/cert.pem" \
    -name "$IDENTITY" -passout env:PASS -out "$dir/identity.p12"
}

main() {
  local yes=false
  for arg in "$@"; do
    case $arg in
      --yes | -y) yes=true ;;
      -h | --help)
        echo "usage: $0 [--yes]"
        return 0
        ;;
      *)
        echo "unknown argument: $arg (usage: $0 [--yes])" >&2
        return 2
        ;;
    esac
  done

  local chain state trust=${KERYX_SIGNING_TRUST:-1}
  chain=$(keychain)
  state=$(identity_state "$chain" "$IDENTITY")
  if [[ $state == valid || ($state == untrusted && $trust == 0) ]]; then
    echo "\"$IDENTITY\" is already in $chain; nothing to do."
    return 0
  fi

  if ! $yes; then
    if [[ $state == missing ]]; then
      echo "This adds a self-signed code-signing certificate, \"$IDENTITY\", and its key to"
      if [[ $trust == 0 ]]; then
        echo "$chain, without trusting it (KERYX_SIGNING_TRUST=0)."
      else
        echo "$chain, and trusts it for code signing only. macOS asks for your password."
      fi
    else
      echo "\"$IDENTITY\" is in $chain but not trusted for code signing, so codesign"
      echo "will not use it. This trusts it for code signing only. macOS asks for your password."
    fi
    local answer
    read -r -p "Go ahead? [y/N] " answer
    if [[ ! $answer =~ ^[Yy]([Ee][Ss])?$ ]]; then
      echo "Nothing changed."
      return 1
    fi
  fi

  local dir
  dir=$(mktemp -d "${TMPDIR:-/tmp}/keryx-signing.XXXXXX")
  chmod 700 "$dir"
  # shellcheck disable=SC2064  # the folder is fixed now
  trap "rm -rf '$dir'" EXIT

  if [[ $state == missing ]]; then
    # A throwaway passphrase for the moment the .p12 exists; the file goes with the folder.
    local pass
    pass=$(/usr/bin/openssl rand -hex 24)
    make_certificate "$dir" "$pass"
    security import "$dir/identity.p12" -k "$chain" -f pkcs12 -P "$pass" -T /usr/bin/codesign >/dev/null
    echo "Added \"$IDENTITY\" to $chain."
  else
    security find-certificate -c "$IDENTITY" -p "$chain" >"$dir/cert.pem"
  fi

  if [[ $trust == 0 ]]; then
    echo "Not trusted (KERYX_SIGNING_TRUST=0): codesign will not use it."
    return 0
  fi
  security add-trusted-cert -r trustRoot -p codeSign -k "$chain" "$dir/cert.pem"
  if [[ $(identity_state "$chain" "$IDENTITY") != valid ]]; then
    echo "error: \"$IDENTITY\" is in $chain but codesign still cannot use it." >&2
    return 1
  fi
  echo "\"$IDENTITY\" is ready: build-app.sh signs with it from now on."
  echo "The first build asks whether codesign may use the key: choose Always Allow."
}

# Sourced (to test identity_state), it only defines the functions.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

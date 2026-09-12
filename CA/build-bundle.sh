#!/usr/bin/env bash
# Build the shared trust bundle that lets containers verify the homelab CA.
#
# Why one bundle: `SSL_CERT_FILE` *replaces* the default trust store, while
# `NODE_EXTRA_CA_CERTS` *appends* to it. A single file containing the system
# roots plus the homelab CA satisfies both, so every runtime can be pointed at
# the same path with its own variable name instead of each service carrying its
# own copy.
#
# The bundle is world-readable on purpose: containers run as unrelated UIDs and
# a CA certificate is public information, so no ownership juggling is needed.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
ENV_FILE=${ENV_FILE:-$ROOT_DIR/.env}
CHECK_ONLY=0

usage() {
  cat <<'EOF'
Usage: ./build-bundle.sh [--check] [--env-file PATH]

  --check   Verify the bundle exists and is current without writing anything.
            Exits 3 when the bundle is missing or stale.
EOF
}

while (($#)); do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    --env-file) shift; ENV_FILE=${1:?missing path after --env-file} ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[[ -f "$ENV_FILE" ]] || { echo "Environment file not found: $ENV_FILE" >&2; exit 1; }

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${DATA_BASE:?DATA_BASE is required}"

CA_CERT=${CA_CERT:-$ROOT_DIR/CA/Yiklek CA.crt}
SYSTEM_CA=${SYSTEM_CA:-/etc/ssl/certs/ca-certificates.crt}
BUNDLE_DIR=${CA_BUNDLE_DIR:-$DATA_BASE/ca}
BUNDLE=${CA_BUNDLE_PATH:-$BUNDLE_DIR/ca-bundle.crt}

for path in "$CA_CERT" "$SYSTEM_CA"; do
  [[ -r "$path" ]] || { echo "Not readable: $path" >&2; exit 1; }
done

expected() {
  cat "$SYSTEM_CA" "$CA_CERT"
}

if ((CHECK_ONLY)); then
  [[ -f "$BUNDLE" ]] || { echo "DRIFT missing $BUNDLE" >&2; exit 3; }
  if ! expected | cmp -s - "$BUNDLE"; then
    echo "DRIFT $BUNDLE is stale (rerun build-bundle.sh)" >&2
    exit 3
  fi
  mode=$(stat -c '%a' "$BUNDLE" 2>/dev/null || echo "?")
  [[ "$mode" == "644" ]] || { echo "DRIFT $BUNDLE mode is $mode, expected 644" >&2; exit 3; }
  echo "OK    $BUNDLE is current ($(grep -c 'BEGIN CERTIFICATE' "$BUNDLE") certificates)"
  exit 0
fi

install -d -m 0755 "$BUNDLE_DIR"
tmp=$(mktemp "$BUNDLE_DIR/.ca-bundle.XXXXXX")
trap 'rm -f "$tmp"' EXIT
expected > "$tmp"
install -m 0644 "$tmp" "$BUNDLE"

echo "OK    wrote $BUNDLE ($(grep -c 'BEGIN CERTIFICATE' "$BUNDLE") certificates)"

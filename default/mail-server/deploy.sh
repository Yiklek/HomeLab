#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "$SCRIPT_DIR/../.." && pwd)
ENV_FILE=${ENV_FILE:-$ROOT_DIR/.env}
DRY_RUN=0
CHECK_ONLY=0

usage() {
  cat <<'EOF'
Usage: ./deploy.sh [--dry-run|--check] [--env-file PATH]

  --dry-run   Show filesystem, Authentik and Stalwart changes without applying.
  --check     Exit non-zero when configuration drift is detected.
EOF
}

while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --check) CHECK_ONLY=1 ;;
    --env-file) shift; ENV_FILE=${1:?missing path after --env-file} ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if ((DRY_RUN && CHECK_ONLY)); then
  echo "--dry-run and --check are mutually exclusive" >&2
  exit 2
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Environment file not found: $ENV_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${DOMAIN:?DOMAIN is required}"
: "${DATA_BASE:?DATA_BASE is required}"
: "${MAIL_SERVER_ADMIN:?MAIL_SERVER_ADMIN is required}"
: "${MAIL_OIDC_CLIENT_ID:?MAIL_OIDC_CLIENT_ID is required}"

MAIL_DATA_DIR=${MAIL_DATA_DIR:-$DATA_BASE/mail-server}
MAIL_CERT_SOURCE=${MAIL_CERT_SOURCE:-$DATA_BASE/traefik/traefik.crt}
MAIL_KEY_SOURCE=${MAIL_KEY_SOURCE:-$DATA_BASE/traefik/traefik.key}
COMPOSE_FILE=${COMPOSE_FILE:-$ROOT_DIR/compose.yaml}
MAIL_HELPER_IMAGE=${MAIL_HELPER_IMAGE:-stalwartlabs/stalwart:v0.16}

prepare_files() {
  for path in "$MAIL_CERT_SOURCE" "$MAIL_KEY_SOURCE"; do
    [[ -f "$path" ]] || { echo "Required file not found: $path" >&2; return 1; }
  done
  if ((DRY_RUN)); then
    echo "PLAN  build the shared CA bundle via CA/build-bundle.sh"
    echo "PLAN  use Docker helper $MAIL_HELPER_IMAGE (UID 0 inside container only)"
    echo "PLAN  create $MAIL_DATA_DIR/{etc,data,certs} owned by 2000:2000"
    echo "PLAN  install mail certificate and key"
    return
  fi

  # One shared trust bundle serves every container that has to verify the
  # homelab CA; CA/build-bundle.sh explains why it also carries the system
  # roots. Stalwart reads it through SSL_CERT_FILE.
  if ((CHECK_ONLY)); then
    "$ROOT_DIR/CA/build-bundle.sh" --env-file "$ENV_FILE" --check || return 3
  else
    "$ROOT_DIR/CA/build-bundle.sh" --env-file "$ENV_FILE"
  fi

  docker image inspect "$MAIL_HELPER_IMAGE" >/dev/null 2>&1 || {
    echo "Docker helper image is unavailable: $MAIL_HELPER_IMAGE" >&2
    echo "Load/pull the mail image before running deploy.sh." >&2
    return 1
  }

  if ((CHECK_ONLY)); then
    [[ -d "$MAIL_DATA_DIR" ]] || { echo "DRIFT missing $MAIL_DATA_DIR" >&2; return 3; }
    docker run --rm --network none --user 0:0 --entrypoint /bin/sh \
      -v "$MAIL_DATA_DIR:/target:ro" \
      -v "$MAIL_CERT_SOURCE:/source/mail.crt:ro" \
      -v "$MAIL_KEY_SOURCE:/source/mail.key:ro" \
      "$MAIL_HELPER_IMAGE" -c '
        test -f /target/certs/mail.crt &&
        test -f /target/certs/mail.key &&
        cmp -s /source/mail.crt /target/certs/mail.crt &&
        cmp -s /source/mail.key /target/certs/mail.key
      ' || { echo "DRIFT certificate files are missing or differ" >&2; return 3; }
    echo "OK    certificate files"
    return
  fi

  # Docker already has the privilege required to manage bind-mount ownership;
  # avoid host privilege escalation and host-wide user/group changes.
  docker run --rm --network none --user 0:0 --entrypoint /bin/sh \
    -v "$MAIL_DATA_DIR:/target" \
    -v "$MAIL_CERT_SOURCE:/source/mail.crt:ro" \
    -v "$MAIL_KEY_SOURCE:/source/mail.key:ro" \
    "$MAIL_HELPER_IMAGE" -c '
      set -eu
      mkdir -p /target/etc /target/data /target/certs
      cp /source/mail.crt /target/certs/mail.crt
      cp /source/mail.key /target/certs/mail.key
      rm -f /target/certs/ca-bundle.crt
      chown -R 2000:2000 /target/etc /target/data /target/certs
      chmod 750 /target/etc /target/data /target/certs
      chmod 644 /target/certs/mail.crt
      chmod 600 /target/certs/mail.key
    '
}

wait_jmap() {
  local i
  for i in $(seq 1 90); do
    if curl --noproxy '*' --connect-timeout 2 --max-time 5 -fsS \
      -u "$MAIL_SERVER_ADMIN" http://127.0.0.1:8288/jmap/session >/dev/null 2>&1; then
      echo "OK    Stalwart JMAP ready (${i}s)"
      return 0
    fi
    sleep 1
  done
  echo "Stalwart JMAP did not become ready" >&2
  return 1
}

# Stalwart resolves permissions from an account's own role, so group membership
# cannot grant administrator rights by itself. Translate the Authentik group
# into the explicit account list instead, which keeps the group as the single
# place to manage who administers mail. configure.py only ever promotes, so an
# empty group simply means "nobody gets promoted" rather than a lockout.
resolve_admin_accounts() {
  [[ -n "${MAIL_OIDC_ADMIN_GROUP:-}" ]] || return 0
  local members
  members=$("$SCRIPT_DIR/configure-authentik.py" --env-file "$ENV_FILE" \
    --print-members "$MAIL_OIDC_ADMIN_GROUP" 2>/dev/null | tr '\n' ',' | sed 's/,$//')
  if [[ -n "$members" ]]; then
    export MAIL_OIDC_ADMIN_ACCOUNTS="$members"
    echo "OK    Stalwart administrators from group $MAIL_OIDC_ADMIN_GROUP: $members"
  else
    export MAIL_OIDC_ADMIN_ACCOUNTS=""
    echo "WARN  group $MAIL_OIDC_ADMIN_GROUP has no members; nothing will be promoted" >&2
  fi
}

LAST_CHANGED=0
run_step() {
  set +e
  "$@"
  local status=$?
  set -e
  case "$status" in
    0) LAST_CHANGED=0 ;;
    10) LAST_CHANGED=1 ;;
    *) exit "$status" ;;
  esac
}

prepare_files
if ((DRY_RUN)); then
  echo "PLAN  docker compose up -d mail webmail"
elif ((CHECK_ONLY)); then
  wait_jmap
else
  # Both services share the trust bundle this script builds, so bring them
  # up together to keep the file present before the webmail starts.
  docker compose -f "$COMPOSE_FILE" up -d mail webmail
  wait_jmap
fi

if ((CHECK_ONLY)); then
  "$SCRIPT_DIR/configure-authentik.py" --env-file "$ENV_FILE" --check
  resolve_admin_accounts
  "$SCRIPT_DIR/configure.py" bootstrap --env-file "$ENV_FILE" --check
  "$SCRIPT_DIR/configure.py" apply --env-file "$ENV_FILE" --check
  exit 0
fi

if ((DRY_RUN)); then
  if ! curl --noproxy '*' --connect-timeout 2 --max-time 5 -fsS \
    -u "$MAIL_SERVER_ADMIN" http://127.0.0.1:8288/jmap/session >/dev/null 2>&1; then
    echo "SKIP  live Authentik/Stalwart reconciliation (JMAP is not running)"
    echo "      Run --dry-run again after the first container start for an object-level plan."
    exit 0
  fi
  "$SCRIPT_DIR/configure-authentik.py" --env-file "$ENV_FILE" --dry-run
  resolve_admin_accounts
  "$SCRIPT_DIR/configure.py" bootstrap --env-file "$ENV_FILE" --dry-run
  "$SCRIPT_DIR/configure.py" apply --env-file "$ENV_FILE" --dry-run
  exit 0
fi

run_step "$SCRIPT_DIR/configure.py" bootstrap --env-file "$ENV_FILE"
if ((LAST_CHANGED)); then
  docker restart mail-server >/dev/null
  wait_jmap
fi

changed=0
run_step "$SCRIPT_DIR/configure-authentik.py" --env-file "$ENV_FILE"
((LAST_CHANGED)) && changed=1
resolve_admin_accounts
run_step "$SCRIPT_DIR/configure.py" apply --env-file "$ENV_FILE"
((LAST_CHANGED)) && changed=1
if ((changed)); then
  docker restart mail-server >/dev/null
  wait_jmap
fi

"$SCRIPT_DIR/configure.py" apply --env-file "$ENV_FILE" --check
"$SCRIPT_DIR/configure-authentik.py" --env-file "$ENV_FILE" --check

echo "Mail server deployment and configuration are converged."

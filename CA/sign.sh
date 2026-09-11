#!/usr/bin/env bash
#
# sign.sh - issue a leaf certificate from a local CA.
#
# Includes:
#   - CA_NAME SERVICE DOMAIN DAYS [EXTFILE]
# Options:
#   --init-ca, --init-extfile, --profile, --san, --show-extfile, --dry-run
#
# See --help for details. Exit codes: 0 ok, 1 runtime error, 2 usage error.

set -euo pipefail

umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

INIT_CA=0
INIT_EXTFILE=0
PROFILE=server
SHOW_EXTFILE=0
DRY_RUN=0
SAN_SPECS=()

usage() {
    cat >&2 <<'EOF'
Usage: sign.sh [options] CA_NAME SERVICE DOMAIN DAYS [EXTFILE]

Issue SERVICE.crt signed by CA_NAME, using EXTFILE (default: extfile).

Arguments:
  CA_NAME   CA basename, e.g. "Yiklek CA"
  SERVICE   Leaf basename, e.g. traefik
  DOMAIN    Base domain used for the CSR CN, e.g. lab.home
  DAYS      Leaf validity in days (positive integer)
  EXTFILE   OpenSSL extension file (default: extfile)

Options:
  --init-ca             Create CA_NAME.crt/.key when both are missing.
                        Without this flag a missing CA is fatal, so a typo
                        can never silently mint a new, untrusted CA.
  --init-extfile        Create EXTFILE from --profile when it is missing.
  --profile NAME        Template used by --init-extfile: server (default),
                        client, or mutual.
  --san SPEC            Replace subjectAltName. Repeatable and comma
                        separated. SPEC is DNS:name, IP:addr, email:addr,
                        URI:url, or a bare value that is auto-detected.
  --show-extfile        Print the effective extension file.
  --dry-run             Validate and print, but do not sign.
  -h, --help            Show this help.

An existing SERVICE.crt is moved to SERVICE.<timestamp>.crt.bak before it is
replaced.
EOF
}

die() { echo "error: $*" >&2; exit 1; }
usage_error() { usage; exit 2; }

is_ipv4() {
    [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
    local IFS=. o
    for o in $1; do
        [ "$((10#$o))" -le 255 ] || return 1
    done
    return 0
}

is_ipv6() {
    [[ "$1" == *:* ]] || return 1
    [[ "$1" =~ ^[0-9A-Fa-f:]+$ ]] || return 1
    [[ "$1" == *:*:* ]] || return 1
    return 0
}

is_dns() {
    local n=$1
    [[ "$n" == *:* ]] && return 1
    [[ "$n" == *" "* ]] && return 1
    if [[ "$n" == \*. ]]; then return 1; fi
    [[ "$n" == \*. ]] || true
    case "$n" in \*.*) n=${n#\*.} ;; esac
    [ -n "$n" ] || return 1
    [[ "$n" =~ ^[A-Za-z0-9_]([A-Za-z0-9_-]*[A-Za-z0-9_])?(\.[A-Za-z0-9_]([A-Za-z0-9_-]*[A-Za-z0-9_])?)*$ ]]
}

while [ $# -gt 0 ]; do
    case "$1" in
        --init-ca)      INIT_CA=1; shift ;;
        --init-extfile) INIT_EXTFILE=1; shift ;;
        --profile)
            [ $# -ge 2 ] || usage_error
            PROFILE=$2; shift 2 ;;
        --san)
            [ $# -ge 2 ] || usage_error
            SAN_SPECS+=("$2"); shift 2 ;;
        --show-extfile) SHOW_EXTFILE=1; shift ;;
        --dry-run)      DRY_RUN=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        --)             shift; break ;;
        -*)             usage_error ;;
        *)              break ;;
    esac
done

[ $# -ge 4 ] && [ $# -le 5 ] || usage_error

CA_NAME=$1
SERVICE=$2
DOMAIN=$3
DAYS=$4
EXTFILE=${5:-extfile}

case "$PROFILE" in
    server|client|mutual) ;;
    *) die "unknown profile: $PROFILE (expected server, client or mutual)" ;;
esac

[ -n "$CA_NAME" ] && [ -n "$SERVICE" ] && [ -n "$DOMAIN" ] \
    || die "CA_NAME, SERVICE and DOMAIN must not be empty"
[ "$CA_NAME" != "$SERVICE" ] || die "SERVICE must differ from CA_NAME"
[[ "$DAYS" =~ ^[1-9][0-9]*$ ]] || die "DAYS must be a positive integer, got: $DAYS"
case "$CA_NAME$SERVICE$DOMAIN" in
    */*|*..*) die "CA_NAME, SERVICE and DOMAIN must not contain '/' or '..'" ;;
esac

tmp_ext=""
tmp_crt=""
cleanup() {
    if [ -n "$tmp_ext" ]; then rm -f -- "$tmp_ext"; fi
    if [ -n "$tmp_crt" ]; then rm -f -- "$tmp_crt"; fi
    return 0
}
trap cleanup EXIT

# --- CA --------------------------------------------------------------------
ca_crt="${CA_NAME}.crt"
ca_key="${CA_NAME}.key"

if [ -f "$ca_crt" ] && [ -f "$ca_key" ]; then
    :
elif [ -f "$ca_crt" ] || [ -f "$ca_key" ]; then
    die "CA is incomplete: both ${ca_crt} and ${ca_key} are required"
elif [ "$INIT_CA" -eq 1 ]; then
    echo "Generating CA ${CA_NAME}..."
    openssl req -x509 -noenc -sha256 -days 3650 -newkey rsa:4096 \
        -keyout "$ca_key" -out "$ca_crt" \
        -subj "/C=CN/ST=Beijing/L=Beijing/CN=${CA_NAME}" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -addext "subjectKeyIdentifier=hash"
else
    die "CA ${CA_NAME} not found; re-run with --init-ca to create it"
fi
chmod 600 "$ca_key"
chmod 644 "$ca_crt"

# --- CSR -------------------------------------------------------------------
if [ ! -f "${SERVICE}.csr" ]; then
    if [ -f "${SERVICE}.key" ]; then
        echo "Regenerating CSR for ${SERVICE} from the existing key"
        openssl req -new -sha256 -key "${SERVICE}.key" -out "${SERVICE}.csr" \
            -subj "/C=CN/ST=Beijing/L=Beijing/CN=*.${DOMAIN}"
    else
        echo "Generating key and CSR for ${SERVICE}"
        openssl req -new -sha256 -newkey rsa:2048 -noenc \
            -keyout "${SERVICE}.key" -out "${SERVICE}.csr" \
            -subj "/C=CN/ST=Beijing/L=Beijing/CN=*.${DOMAIN}"
    fi
fi
[ -f "${SERVICE}.key" ] || die "missing ${SERVICE}.key"
chmod 600 "${SERVICE}.key"

if ! diff -q <(openssl req -in "${SERVICE}.csr" -noout -pubkey) \
             <(openssl pkey -in "${SERVICE}.key" -pubout) >/dev/null; then
    die "${SERVICE}.csr does not match ${SERVICE}.key"
fi

# --- Extension file --------------------------------------------------------
if [ ! -f "$EXTFILE" ]; then
    if [ "$INIT_EXTFILE" -ne 1 ]; then
        die "extension file not found: $EXTFILE (use --init-extfile to create it)"
    fi
    case "$PROFILE" in
        server) ku="critical,digitalSignature,keyEncipherment"; eku="serverAuth" ;;
        client) ku="critical,digitalSignature";                  eku="clientAuth" ;;
        mutual) ku="critical,digitalSignature,keyEncipherment";  eku="serverAuth,clientAuth" ;;
    esac
    echo "Generating extension file ${EXTFILE} (profile ${PROFILE})"
    cat > "$EXTFILE" <<EOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=critical,CA:FALSE
keyUsage=${ku}
extendedKeyUsage=${eku}
subjectKeyIdentifier=hash
subjectAltName=@alt_names
[alt_names]
DNS.1=${DOMAIN}
DNS.2=*.${DOMAIN}
EOF
fi
chmod 644 "$EXTFILE"

effective_ext=$EXTFILE

# --- Optional subjectAltName override -------------------------------------
if [ ${#SAN_SPECS[@]} -gt 0 ]; then
    declare -A san_seen=()
    san_list=()

    add_san() {
        local spec=$1 part type value
        local IFS=','
        for part in $spec; do
            part="${part#"${part%%[![:space:]]*}"}"
            part="${part%"${part##*[![:space:]]}"}"
            [ -n "$part" ] || die "empty entry in --san '$spec'"
            case "$part" in
                DNS:*|IP:*|email:*|URI:*)
                    type=${part%%:*}; value=${part#*:} ;;
                *://*)
                    type=URI;   value=$part ;;
                *@*)
                    type=email; value=$part ;;
                *)
                    value=$part
                    if is_ipv4 "$part"; then
                        type=IP
                    elif [[ "$part" == *:* ]]; then
                        is_ipv6 "$part" || die "not a valid IP address: '$part'"
                        type=IP
                    else
                        type=DNS
                    fi ;;
            esac
            case "$type" in
                DNS)   is_dns "$value" || die "not a valid DNS name: '$value'" ;;
                IP)    if ! is_ipv4 "$value" && ! is_ipv6 "$value"; then
                           die "not a valid IP address: '$value'"
                       fi ;;
                email) [[ "$value" == *@* && "$value" != *" "* ]] \
                           || die "not a valid email SAN: '$value'" ;;
                URI)   [[ "$value" == *://* ]] || die "not a valid URI SAN: '$value'" ;;
                *)     die "unsupported SAN type '$type' in '$part'" ;;
            esac
            if [ -z "${san_seen["${type}:${value}"]+x}" ]; then
                san_seen["${type}:${value}"]=1
                san_list+=("${type}:${value}")
            fi
        done
    }

    for spec in "${SAN_SPECS[@]}"; do
        add_san "$spec"
    done
    [ ${#san_list[@]} -gt 0 ] || die "--san did not yield any entry"

    tmp_ext=$(mktemp "./${SERVICE}.ext.XXXXXX")
    awk '
        /^[[:space:]]*\[/ {
            hdr = $0
            gsub(/[[:space:]]/, "", hdr)
            if (tolower(hdr) == "[alt_names]") { skip = 1; next }
            skip = 0
        }
        skip { next }
        /^[[:space:]]*subjectAltName[[:space:]]*=/ { next }
        { print }
    ' "$EXTFILE" > "$tmp_ext"
    {
        echo "subjectAltName=@alt_names"
        echo "[alt_names]"
        n_dns=0; n_ip=0; n_mail=0; n_uri=0
        for entry in "${san_list[@]}"; do
            t=${entry%%:*}
            v=${entry#*:}
            case "$t" in
                DNS)   n_dns=$((n_dns + 1));   echo "DNS.${n_dns}=${v}" ;;
                IP)    n_ip=$((n_ip + 1));     echo "IP.${n_ip}=${v}" ;;
                email) n_mail=$((n_mail + 1)); echo "email.${n_mail}=${v}" ;;
                URI)   n_uri=$((n_uri + 1));   echo "URI.${n_uri}=${v}" ;;
            esac
        done
    } >> "$tmp_ext"
    effective_ext=$tmp_ext
fi

if [ "$SHOW_EXTFILE" -eq 1 ]; then
    echo "--- effective extension file: ${effective_ext} ---"
    cat "$effective_ext"
    echo "--- end of extension file ---"
fi

# --- Effective extension validation ---------------------------------------
if ! grep -qiE '^[[:space:]]*basicConstraints' "$effective_ext"; then
    die "${effective_ext} does not set basicConstraints"
fi
if ! grep -qi 'CA:false' "$effective_ext"; then
    die "${effective_ext} must set basicConstraints CA:FALSE"
fi
if ! grep -qiE '^[[:space:]]*subjectAltName' "$effective_ext"; then
    echo "warning: ${effective_ext} sets no subjectAltName; modern clients ignore CN" >&2
fi

if [ "$DRY_RUN" -eq 1 ]; then
    echo "dry-run: validation succeeded, nothing was signed"
    exit 0
fi

# --- Leaf validity guard against the CA -----------------------------------
ca_notafter=$(openssl x509 -in "$ca_crt" -noout -enddate | cut -d= -f2)
ca_epoch=$(date -d "$ca_notafter" +%s) || die "cannot parse CA expiry: $ca_notafter"
now_epoch=$(date +%s)
max_days=$(( (ca_epoch - now_epoch) / 86400 ))
[ "$max_days" -gt 0 ] || die "CA ${CA_NAME} is already expired"
[ "$DAYS" -le "$max_days" ] \
    || die "DAYS=${DAYS} exceeds the CA's remaining validity (${max_days}d)"

# --- Serial number --------------------------------------------------------
ca_srl="${CA_NAME}.srl"
if [ ! -s "$ca_srl" ]; then
    openssl rand -hex 16 > "$ca_srl"
fi
chmod 600 "$ca_srl"

# --- Sign -----------------------------------------------------------------
tmp_crt=$(mktemp "./${SERVICE}.crt.XXXXXX")
echo "Signing ${SERVICE} for ${DAYS} days"
openssl x509 -req -sha256 -days "$DAYS" \
    -in "${SERVICE}.csr" -out "$tmp_crt" \
    -CA "$ca_crt" -CAkey "$ca_key" \
    -CAserial "$ca_srl" -extfile "$effective_ext"

if ! openssl verify -CAfile "$ca_crt" "$tmp_crt" >/dev/null 2>&1; then
    die "issued certificate failed chain verification"
fi
if ! diff -q <(openssl x509 -in "$tmp_crt" -noout -pubkey) \
             <(openssl pkey -in "${SERVICE}.key" -pubout) >/dev/null; then
    die "issued certificate public key does not match ${SERVICE}.key"
fi

if [ -f "${SERVICE}.crt" ]; then
    backup="${SERVICE}.$(date +%Y%m%d%H%M%S).crt.bak"
    echo "Backing up ${SERVICE}.crt to ${backup}"
    mv -- "${SERVICE}.crt" "$backup"
fi
mv -- "$tmp_crt" "${SERVICE}.crt"
tmp_crt=""
chmod 644 "${SERVICE}.crt"

echo
echo "Issued ${SERVICE}.crt"
openssl x509 -in "${SERVICE}.crt" -noout -subject -issuer -dates \
    -fingerprint -sha256 \
    -ext basicConstraints,keyUsage,extendedKeyUsage,subjectAltName

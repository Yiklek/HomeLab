#!/usr/bin/env bash
if [ $# -ne 4 ]; then
    echo "Usage: $0 [CA_NAME] [SERVICE] [DOMAIN] [DAYS]"
    exit
fi
CA_NAME=$1
SERVICE=$2
DOMAIN=$3
DAYS=$4

if [ "$CA_NAME" = "$SERVICE" ]; then
    echo "SERVICE should not be equal to CA_NAME"
    exit
fi
if [ ! -f "${CA_NAME}.crt" ]; then
    echo "Genrate CA..."
    openssl req -x509 -noenc -days 3650 -newkey rsa:4096 -keyout "${CA_NAME}.key" -out "${CA_NAME}.crt" -subj "/C=CN/ST=Beijing/L=Beijing/CN=${CA_NAME}"
fi

if [ ! -f "${SERVICE}.csr" ]; then
    echo "Genrate CSR for $SERVICE"
    openssl req -newkey rsa:2048 -noenc -keyout $SERVICE.key -out $SERVICE.csr  -subj=/C=CN/ST=Beijing/L=Beijing/CN=*.${DOMAIN}
fi

cat > extfile << EOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, nonRepudiation, keyEncipherment, dataEncipherment
extendedKeyUsage = clientAuth,serverAuth
subjectAltName = @alt_names
[alt_names]
DNS.1=${DOMAIN}
DNS.2=*.${DOMAIN}
EOF

echo "Signing for $SERVICE"
openssl x509 -req  -days ${DAYS} -CAcreateserial -in $SERVICE.csr -out $SERVICE.crt -CA "${CA_NAME}.crt" -CAkey "${CA_NAME}.key" -extfile extfile

#!/usr/bin/env bash
# Generates real values for every secret placeholder in .env.example /
# .env.device-gateway.example -- run this instead of hand-editing in
# "change-me" text, which is exactly the mistake this script exists to
# prevent (see the checked-in root .env this repo shipped with before:
# SECRET_ENCRYPTION_KEY=change-me-fernet-key-base64 is not valid base64
# and breaks anything that touches encryption the moment it's used).
#
# Usage:
#   ./scripts/generate_secrets.sh            # prints values to stdout
#   ./scripts/generate_secrets.sh --write    # writes .env + .env.device-gateway
#
# Either way, NEVER commit the filled-in .env / .env.device-gateway
# files (Section 19) -- both are already gitignored; this script only
# ever writes to those two paths, never to the tracked .example files.

set -euo pipefail

fernet_key() {
  python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
}

urlsafe_secret() {
  python3 -c "import secrets; print(secrets.token_urlsafe(48))"
}

SECRET_KEY="$(urlsafe_secret)"
SECRET_ENCRYPTION_KEY="$(fernet_key)"
DEVICE_CREDENTIAL_ENCRYPTION_KEY="$(fernet_key)"
DEVICE_JOB_SIGNING_KEY="$(urlsafe_secret)"
POSTGRES_PASSWORD="$(urlsafe_secret)"
NETGUARD_APP_DB_PASSWORD="$(urlsafe_secret)"
NATS_GATEWAY_PASSWORD="$(urlsafe_secret)"

# SECRET_ENCRYPTION_KEY and DEVICE_CREDENTIAL_ENCRYPTION_KEY must be two
# DIFFERENT keys -- that split is the whole point of Section 4's
# device-credential key isolation (see app/core/crypto.py). This script
# always calls fernet_key() twice, independently, so they never collide.

if [ "${1:-}" = "--write" ]; then
  if [ -f .env ] || [ -f .env.device-gateway ]; then
    echo "Refusing to overwrite an existing .env / .env.device-gateway." >&2
    echo "Delete them first if you really want fresh values." >&2
    exit 1
  fi

  sed \
    -e "s#^SECRET_KEY=.*#SECRET_KEY=${SECRET_KEY}#" \
    -e "s#^SECRET_ENCRYPTION_KEY=.*#SECRET_ENCRYPTION_KEY=${SECRET_ENCRYPTION_KEY}#" \
    -e "s#^DEVICE_JOB_SIGNING_KEY=.*#DEVICE_JOB_SIGNING_KEY=${DEVICE_JOB_SIGNING_KEY}#" \
    -e "s#^POSTGRES_PASSWORD=.*#POSTGRES_PASSWORD=${POSTGRES_PASSWORD}#" \
    -e "s#^NETGUARD_APP_DB_PASSWORD=.*#NETGUARD_APP_DB_PASSWORD=${NETGUARD_APP_DB_PASSWORD}#" \
    -e "s#^NATS_GATEWAY_PASSWORD=.*#NATS_GATEWAY_PASSWORD=${NATS_GATEWAY_PASSWORD}#" \
    .env.example > .env

  sed \
    -e "s#^DEVICE_CREDENTIAL_ENCRYPTION_KEY=.*#DEVICE_CREDENTIAL_ENCRYPTION_KEY=${DEVICE_CREDENTIAL_ENCRYPTION_KEY}#" \
    .env.device-gateway.example > .env.device-gateway

  echo "Wrote .env and .env.device-gateway with real generated secrets."
  echo "Still needed by hand: OIDC_*, OPENBAO_ROLE_ID/SECRET_ID (from"
  echo "openbao/bootstrap/bootstrap.sh), and NATS_GATEWAY_USER if you"
  echo "changed it from the nats-server.conf default."
else
  cat <<EOF
SECRET_KEY=${SECRET_KEY}
SECRET_ENCRYPTION_KEY=${SECRET_ENCRYPTION_KEY}
DEVICE_CREDENTIAL_ENCRYPTION_KEY=${DEVICE_CREDENTIAL_ENCRYPTION_KEY}
DEVICE_JOB_SIGNING_KEY=${DEVICE_JOB_SIGNING_KEY}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
NETGUARD_APP_DB_PASSWORD=${NETGUARD_APP_DB_PASSWORD}
NATS_GATEWAY_PASSWORD=${NATS_GATEWAY_PASSWORD}

# Paste these over the matching "change-me..." placeholders in .env /
# .env.device-gateway, or re-run with --write to do it automatically.
EOF
fi
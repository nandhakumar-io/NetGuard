#!/usr/bin/env bash
# One-time OpenBao setup for NetGuard's Device Gateway integration.
#
# Run this AFTER `openbao operator init` + `openbao operator unseal` have
# already happened and you're logged in with a sufficiently-privileged
# token (NOT the root token in day-to-day use -- see the note at the
# bottom). This script does not handle init/unseal itself: that's a
# manual, witnessed, split-key procedure (Section 17 -- "secure
# initialization/unseal procedure, protected recovery/unseal material")
# that deliberately isn't automated here.
#
# What this does:
#   1. Enables a KV v2 secrets engine at netguard-devices/
#   2. Enables the AppRole auth method
#   3. Writes the least-privilege policy (device-gateway-policy.hcl)
#   4. Creates an AppRole bound to that policy, with short-lived,
#      limited-use-count tokens
#   5. Prints the Role ID (safe to put in compose/env) and generates a
#      Secret ID (NOT safe to log/store in plaintext long-term -- see
#      the warning it prints)
#
# Idempotent-ish: safe to re-run steps 1-3; step 4/5 will happily create
# a second Secret ID for the same AppRole if run again -- rotate by
# generating a new Secret ID and revoking the old one
# (`bao write -f auth/approle/role/device-gateway/secret-id-accessor/destroy`),
# not by re-running this whole script.

set -euo pipefail

VAULT_ADDR="${OPENBAO_ADDR:-http://localhost:8200}"
export VAULT_ADDR

echo "== Enabling KV v2 at netguard-devices/ =="
bao secrets enable -path=netguard-devices -version=2 kv || echo "(already enabled, continuing)"

echo "== Enabling AppRole auth =="
bao auth enable approle || echo "(already enabled, continuing)"

echo "== Writing device-gateway policy =="
bao policy write netguard-device-gateway "$(dirname "$0")/../policies/device-gateway-policy.hcl"

echo "== Creating device-gateway AppRole =="
# token_ttl/token_max_ttl short: the Gateway re-logs-in automatically
# (see openbao_client.py's _ensure_token), so there's no operational
# reason for a long-lived token here -- shorter tokens shrink the window
# a stolen one is useful in.
bao write auth/approle/role/device-gateway \
    token_policies="netguard-device-gateway" \
    token_ttl=15m \
    token_max_ttl=1h \
    secret_id_ttl=90d \
    secret_id_num_uses=0

ROLE_ID=$(bao read -field=role_id auth/approle/role/device-gateway/role-id)
SECRET_ID=$(bao write -f -field=secret_id auth/approle/role/device-gateway/secret-id)

echo ""
echo "== Done. Set these on the device-gateway service ONLY (never on api): =="
echo "OPENBAO_ADDR=${VAULT_ADDR}"
echo "OPENBAO_ROLE_ID=${ROLE_ID}"
echo "OPENBAO_SECRET_ID=${SECRET_ID}"
echo ""
echo "WARNING: OPENBAO_SECRET_ID above is a live secret. Inject it via your"
echo "orchestrator's secret mechanism (Docker/Swarm secret, k8s Secret,"
echo "a mounted file read at container start) rather than plain compose"
echo "environment in any real deployment -- this script prints it to stdout"
echo "once, deliberately, for you to move it directly into that mechanism,"
echo "not to store or paste it elsewhere."

# --- Migrating an existing device credential into OpenBao -------------
# For each device currently resolved via the DB-Fernet path
# (Device.ssh_password_encrypted) or the legacy NETGUARD_CRED_<REF> env
# fallback, write it to the path credential_service._try_openbao()
# reads (netguard-devices/data/<ssh_credential_ref>):
#
#   bao kv put netguard-devices/<ssh_credential_ref> \
#       username="<device ssh username>" \
#       password="<device ssh password>"
#
# credential_service.get_ssh_password() checks OpenBao first and falls
# back to the existing DB/env path automatically, so devices can be
# migrated one at a time with zero downtime -- there's no cutover event,
# just progressively fewer devices falling through to the legacy path.
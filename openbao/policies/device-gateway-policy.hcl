# Least-privilege policy bound to the device-gateway AppRole (Section 4/17).
#
# Deliberately scoped to ONLY the device-credentials KV mount, and to
# read/list -- not write/delete. The Gateway resolves credentials that
# were provisioned out-of-band (device onboarding flow / migration
# script in bootstrap.sh); it has no business creating or destroying
# secrets, so that capability isn't granted here even though the Gateway
# is a "trusted" service. This is what makes "a compromised Gateway
# cannot rewrite/delete every stored device credential" true.

path "netguard-devices/data/*" {
  capabilities = ["read"]
}

path "netguard-devices/metadata/*" {
  capabilities = ["list"]
}

# Explicitly nothing else: no sys/, no auth/, no other mounts. A policy
# with no matching path for a request is denied by default in
# OpenBao/Vault -- there is no need for an explicit "deny *" block.

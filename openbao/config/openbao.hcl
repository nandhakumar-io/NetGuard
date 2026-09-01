# OpenBao server config (Section 17). File storage backend -- fine for a
# single-node deployment; move to Raft/integrated storage or an HA
# backend before running more than one OpenBao replica.
#
# TLS is intentionally left disabled on the listener here because OpenBao
# sits entirely on netguard-secrets/netguard-internal, never on the proxy
# network, and the compose file gives it no published port -- see
# docker-compose.yaml's comment on the `openbao` service for the network
# ACL reasoning. Terminate TLS at the listener instead if OpenBao is ever
# reachable from a network you don't fully trust.

ui = false

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}

storage "file" {
  path = "/vault/data"
}

# Disabled: set to true for containerised deployments -- even with IPC_LOCK
# granted, the kernel's seccomp/AppArmor profile commonly blocks mlock(2)
# inside a container, causing a hard exit.  Secrets-at-rest protection is
# provided by the volume encryption of the underlying host storage instead.
disable_mlock = true

api_addr     = "http://openbao:8200"
cluster_addr = "http://openbao:8201"

log_level = "info"

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

# Disabled: mlock is handled via the IPC_LOCK capability granted in
# docker-compose.yaml instead of being turned off here, so secrets still
# don't get swapped to disk.
disable_mlock = false

api_addr     = "http://openbao:8200"
cluster_addr = "http://openbao:8201"

log_level = "info"

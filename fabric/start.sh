#!/usr/bin/env bash
# start.sh — bring up the dev Fabric network and create
# netguard-audit-channel (spec Section 5/6). Run scripts/bootstrap.sh
# first (once) to generate crypto material and channel artifacts.
#
# This script does NOT deploy chaincode -- run deploy-chaincode.sh
# afterwards. It also does not build/start the fabric-gateway sidecar;
# that container's build context (fabric/gateway/) is a separate piece
# of work and is intentionally not included yet, so `docker compose up`
# here is scoped to the ledger components only (orderer, both peers,
# both CouchDBs) via --no-deps-style service selection rather than
# failing the whole network on a missing gateway build.
set -euo pipefail

FABRIC_VERSION="2.5.9"   # must match scripts/bootstrap.sh and network/docker-compose.yaml image pins
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FABRIC_ROOT="$(cd "${HERE}/.." && pwd)"
NETWORK_DIR="${FABRIC_ROOT}/network"

LEDGER_SERVICES=(
  orderer.orderer.netguard.local
  peer0.netguardorg.netguard.local
  peer0.auditorg.netguard.local
  couchdb.netguardorg
  couchdb.auditorg
)

if [[ ! -f "${NETWORK_DIR}/channel-artifacts/genesis.block" ]]; then
  echo "channel-artifacts/genesis.block not found -- run scripts/bootstrap.sh first" >&2
  exit 1
fi

if ! docker network inspect netguard-internal >/dev/null 2>&1; then
  echo "netguard-internal Docker network not found -- start the main NetGuard stack first (docker/docker-compose.yaml) so fabric-gateway has something to attach to later" >&2
  exit 1
fi

echo "== starting Fabric ledger services (orderer, peers, CouchDB) =="
(cd "${NETWORK_DIR}" && docker compose up -d "${LEDGER_SERVICES[@]}")

echo "-- waiting for peers to report healthy"
for svc in peer0.netguardorg.netguard.local peer0.auditorg.netguard.local; do
  for _ in $(seq 1 30); do
    status="$(docker inspect --format '{{.State.Health.Status}}' "${svc}" 2>/dev/null || echo starting)"
    [[ "${status}" == "healthy" ]] && break
    sleep 2
  done
  if [[ "${status:-}" != "healthy" ]]; then
    echo "${svc} did not become healthy in time -- check 'docker logs ${svc}'" >&2
    exit 1
  fi
done

CLI_IMAGE="hyperledger/fabric-tools:${FABRIC_VERSION}"
NETGUARDORG_MSP="${FABRIC_ROOT}/organizations/peerOrganizations/netguardorg.netguard.local"
AUDITORG_MSP="${FABRIC_ROOT}/organizations/peerOrganizations/auditorg.netguard.local"
ORDERER_CA="${FABRIC_ROOT}/organizations/ordererOrganizations/orderer.netguard.local/orderers/orderer.orderer.netguard.local/tls/ca.crt"
CHANNEL_TX="${NETWORK_DIR}/channel-artifacts/netguard-audit-channel.tx"

# peer channel create/join run from a short-lived fabric-tools container
# on netguard-fabric rather than requiring the peer/osnadmin binaries on
# the host -- keeps this script's only host dependency at "docker".
docker run --rm \
  --network netguard-fabric \
  -e CORE_PEER_TLS_ENABLED=true \
  -e CORE_PEER_LOCALMSPID=NetGuardOrgMSP \
  -e CORE_PEER_MSPCONFIGPATH=/msp/netguardorg/users/Admin@netguardorg.netguard.local/msp \
  -e CORE_PEER_TLS_ROOTCERT_FILE=/msp/netguardorg/peers/peer0.netguardorg.netguard.local/tls/ca.crt \
  -e CORE_PEER_ADDRESS=peer0.netguardorg.netguard.local:7051 \
  -v "${NETGUARDORG_MSP}:/msp/netguardorg:ro" \
  -v "${AUDITORG_MSP}:/msp/auditorg:ro" \
  -v "${ORDERER_CA}:/msp/orderer-ca.crt:ro" \
  -v "${CHANNEL_TX}:/channel-artifacts/netguard-audit-channel.tx:ro" \
  "${CLI_IMAGE}" \
  peer channel create \
    -o orderer.orderer.netguard.local:7050 \
    -c netguard-audit-channel \
    -f /channel-artifacts/netguard-audit-channel.tx \
    --outputBlock /channel-artifacts/netguard-audit-channel.block \
    --tls --cafile /msp/orderer-ca.crt

for ORG in netguardorg:7051:NetGuardOrgMSP auditorg:9051:AuditOrgMSP; do
  ORG_DIR="${ORG%%:*}"; REST="${ORG#*:}"; PEER_PORT="${REST%%:*}"; MSP_ID="${REST#*:}"
  echo "-- joining peer0.${ORG_DIR}.netguard.local to netguard-audit-channel"
  docker run --rm \
    --network netguard-fabric \
    -e CORE_PEER_TLS_ENABLED=true \
    -e CORE_PEER_LOCALMSPID="${MSP_ID}" \
    -e CORE_PEER_MSPCONFIGPATH="/msp/${ORG_DIR}/users/Admin@${ORG_DIR}.netguard.local/msp" \
    -e CORE_PEER_TLS_ROOTCERT_FILE="/msp/${ORG_DIR}/peers/peer0.${ORG_DIR}.netguard.local/tls/ca.crt" \
    -e CORE_PEER_ADDRESS="peer0.${ORG_DIR}.netguard.local:${PEER_PORT}" \
    -v "${NETGUARDORG_MSP}:/msp/netguardorg:ro" \
    -v "${AUDITORG_MSP}:/msp/auditorg:ro" \
    -v "${NETWORK_DIR}/channel-artifacts:/channel-artifacts:ro" \
    "${CLI_IMAGE}" \
    peer channel join -b /channel-artifacts/netguard-audit-channel.block
done

echo "== netguard-audit-channel is up. Next: ./deploy-chaincode.sh =="
#!/usr/bin/env bash
# bootstrap.sh — one-time generation of the dev network's crypto material
# and channel genesis/config artifacts (spec Section 5/6).
#
# Requires the Fabric binaries (cryptogen, configtxgen) on PATH, pinned
# to FABRIC_VERSION below. Get them via Hyperledger's official install
# script, e.g.:
#   curl -sSL https://raw.githubusercontent.com/hyperledger/fabric/main/scripts/install-fabric.sh | bash -s -- binary
# and add the resulting bin/ dir to PATH before running this script.
set -euo pipefail

FABRIC_VERSION="2.5.9"   # pinned -- see spec Section 6, "Pin compatible Fabric versions. Do not use latest."
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FABRIC_ROOT="$(cd "${HERE}/.." && pwd)"

command -v cryptogen >/dev/null 2>&1 || { echo "cryptogen not found on PATH (need Fabric ${FABRIC_VERSION} binaries)"; exit 1; }
command -v configtxgen >/dev/null 2>&1 || { echo "configtxgen not found on PATH (need Fabric ${FABRIC_VERSION} binaries)"; exit 1; }

echo "== NetGuard Fabric dev network bootstrap (Fabric ${FABRIC_VERSION}) =="

rm -rf "${FABRIC_ROOT}/organizations" "${FABRIC_ROOT}/network/channel-artifacts"
mkdir -p "${FABRIC_ROOT}/organizations" "${FABRIC_ROOT}/network/channel-artifacts"

echo "-- generating MSP/TLS crypto material (NetGuardOrg, AuditOrg, OrdererOrg)"
cryptogen generate \
  --config="${FABRIC_ROOT}/network/crypto-config.yaml" \
  --output="${FABRIC_ROOT}/organizations"

echo "-- generating orderer genesis block"
export FABRIC_CFG_PATH="${FABRIC_ROOT}/network"
configtxgen \
  -profile NetGuardOrdererGenesis \
  -channelID system-channel \
  -outputBlock "${FABRIC_ROOT}/network/channel-artifacts/genesis.block"

echo "-- generating netguard-audit-channel transaction"
configtxgen \
  -profile NetGuardAuditChannel \
  -outputCreateChannelTx "${FABRIC_ROOT}/network/channel-artifacts/netguard-audit-channel.tx" \
  -channelID netguard-audit-channel

echo "-- generating anchor peer updates"
for ORG in NetGuardOrg AuditOrg; do
  configtxgen \
    -profile NetGuardAuditChannel \
    -outputAnchorPeersUpdate "${FABRIC_ROOT}/network/channel-artifacts/${ORG}anchors.tx" \
    -channelID netguard-audit-channel \
    -asOrg "${ORG}MSP"
done

echo "== bootstrap complete. Next: ./start.sh, then ./deploy-chaincode.sh =="
#!/usr/bin/env bash
# deploy-chaincode.sh — package, install, approve, and commit the
# netguard-evidence chaincode on netguard-audit-channel (spec Section 7).
# Run after ./start.sh has created and joined the channel.
#
# Endorsement policy: AND('NetGuardOrgMSP.peer','AuditOrgMSP.peer') --
# both orgs must endorse for a write to commit. This is what makes
# AuditOrg a real independent participant rather than a cosmetic second
# org (see network/configtx.yaml's comment on the same point); it is set
# explicitly at commit time below rather than left to the channel's
# default MAJORITY Endorsement policy, since with exactly two orgs
# MAJORITY and AND happen to coincide today but would silently diverge
# the moment a third org ever joined the channel.
set -euo pipefail

FABRIC_VERSION="2.5.9"
CC_NAME="netguard-evidence"
CC_VERSION="${CC_VERSION:-1.0}"
CC_SEQUENCE="${CC_SEQUENCE:-1}"
CHANNEL_NAME="netguard-audit-channel"
ENDORSEMENT_POLICY="AND('NetGuardOrgMSP.peer','AuditOrgMSP.peer')"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FABRIC_ROOT="$(cd "${HERE}/.." && pwd)"
NETWORK_DIR="${FABRIC_ROOT}/network"
CC_SRC_DIR="${FABRIC_ROOT}/chaincode/${CC_NAME}"
CC_BUILD_DIR="${FABRIC_ROOT}/chaincode/.build/${CC_NAME}"
CLI_IMAGE="hyperledger/fabric-tools:${FABRIC_VERSION}"
GO_IMAGE="golang:1.21"

NETGUARDORG_MSP="${FABRIC_ROOT}/organizations/peerOrganizations/netguardorg.netguard.local"
AUDITORG_MSP="${FABRIC_ROOT}/organizations/peerOrganizations/auditorg.netguard.local"

if [[ ! -d "${NETGUARDORG_MSP}" ]]; then
  echo "organizations/ not found -- run ./bootstrap.sh and ./start.sh first" >&2
  exit 1
fi

echo "== vendoring chaincode dependencies (offline packaging requires a vendor/ dir) =="
rm -rf "${CC_BUILD_DIR}"
mkdir -p "${CC_BUILD_DIR}"
cp -r "${CC_SRC_DIR}/." "${CC_BUILD_DIR}/"
docker run --rm \
  -v "${CC_BUILD_DIR}:/cc" \
  -w /cc \
  -e GOFLAGS=-mod=mod \
  "${GO_IMAGE}" \
  sh -c "go mod tidy && go mod vendor"

peer_exec() {
  local msp_dir="$1" msp_id="$2" peer_addr="$3"; shift 3
  docker run --rm \
    --network netguard-fabric \
    -e CORE_PEER_TLS_ENABLED=true \
    -e CORE_PEER_LOCALMSPID="${msp_id}" \
    -e CORE_PEER_MSPCONFIGPATH="/msp/admin/msp" \
    -e CORE_PEER_TLS_ROOTCERT_FILE=/msp/peer-tls-ca.crt \
    -e CORE_PEER_ADDRESS="${peer_addr}" \
    -v "${msp_dir}/users/Admin@$(basename "${msp_dir}")/msp:/msp/admin/msp:ro" \
    -v "${msp_dir}/peers/$(basename "${peer_addr%%:*}")/tls/ca.crt:/msp/peer-tls-ca.crt:ro" \
    -v "${NETWORK_DIR}/channel-artifacts:/channel-artifacts:ro" \
    -v "${CC_BUILD_DIR}:/cc-package:ro" \
    "${CLI_IMAGE}" "$@"
}

echo "== packaging chaincode =="
docker run --rm \
  -v "${CC_BUILD_DIR}:/cc-package" \
  "${CLI_IMAGE}" \
  peer lifecycle chaincode package "/cc-package/${CC_NAME}.tar.gz" \
    --path "/cc-package" --lang golang --label "${CC_NAME}_${CC_VERSION}"

install_and_approve() {
  local org_dir="$1" msp_id="$2" peer_addr="$3"
  local msp_dir="${FABRIC_ROOT}/organizations/peerOrganizations/${org_dir}"

  echo "-- installing on peer0.${org_dir}"
  peer_exec "${msp_dir}" "${msp_id}" "${peer_addr}" \
    peer lifecycle chaincode install "/cc-package/${CC_NAME}.tar.gz"

  local package_id
  package_id="$(peer_exec "${msp_dir}" "${msp_id}" "${peer_addr}" \
    peer lifecycle chaincode queryinstalled --output json \
    | grep -o "\"package_id\":\"${CC_NAME}_${CC_VERSION}:[a-f0-9]*\"" | head -1 | cut -d'"' -f4)"

  if [[ -z "${package_id}" ]]; then
    echo "failed to determine package_id after install on ${org_dir}" >&2
    exit 1
  fi

  echo "-- approving for ${msp_id} (package_id=${package_id})"
  peer_exec "${msp_dir}" "${msp_id}" "${peer_addr}" \
    peer lifecycle chaincode approveformyorg \
      -o orderer.orderer.netguard.local:7050 --tls \
      --cafile /channel-artifacts/../../organizations/ordererOrganizations/orderer.netguard.local/orderers/orderer.orderer.netguard.local/tls/ca.crt \
      --channelID "${CHANNEL_NAME}" --name "${CC_NAME}" \
      --version "${CC_VERSION}" --sequence "${CC_SEQUENCE}" \
      --signature-policy "${ENDORSEMENT_POLICY}" \
      --package-id "${package_id}"
}

install_and_approve netguardorg.netguard.local NetGuardOrgMSP peer0.netguardorg.netguard.local:7051
install_and_approve auditorg.netguard.local AuditOrgMSP peer0.auditorg.netguard.local:9051

echo "== committing chaincode definition on ${CHANNEL_NAME} =="
peer_exec "${NETGUARDORG_MSP}" NetGuardOrgMSP peer0.netguardorg.netguard.local:7051 \
  peer lifecycle chaincode commit \
    -o orderer.orderer.netguard.local:7050 --tls \
    --cafile /channel-artifacts/../../organizations/ordererOrganizations/orderer.netguard.local/orderers/orderer.orderer.netguard.local/tls/ca.crt \
    --channelID "${CHANNEL_NAME}" --name "${CC_NAME}" \
    --version "${CC_VERSION}" --sequence "${CC_SEQUENCE}" \
    --signature-policy "${ENDORSEMENT_POLICY}" \
    --peerAddresses peer0.netguardorg.netguard.local:7051 \
    --tlsRootCertFiles "${NETGUARDORG_MSP}/peers/peer0.netguardorg.netguard.local/tls/ca.crt" \
    --peerAddresses peer0.auditorg.netguard.local:9051 \
    --tlsRootCertFiles "${AUDITORG_MSP}/peers/peer0.auditorg.netguard.local/tls/ca.crt"

echo "== smoke test: CreateEvidence + GetEvidence =="
SMOKE_ID="EV-SMOKETEST$(date +%s)"
peer_exec "${NETGUARDORG_MSP}" NetGuardOrgMSP peer0.netguardorg.netguard.local:7051 \
  peer chaincode invoke \
    -o orderer.orderer.netguard.local:7050 --tls \
    --cafile /channel-artifacts/../../organizations/ordererOrganizations/orderer.netguard.local/orderers/orderer.orderer.netguard.local/tls/ca.crt \
    -C "${CHANNEL_NAME}" -n "${CC_NAME}" \
    --peerAddresses peer0.netguardorg.netguard.local:7051 \
    --tlsRootCertFiles "${NETGUARDORG_MSP}/peers/peer0.netguardorg.netguard.local/tls/ca.crt" \
    --peerAddresses peer0.auditorg.netguard.local:9051 \
    --tlsRootCertFiles "${AUDITORG_MSP}/peers/peer0.auditorg.netguard.local/tls/ca.crt" \
    -c "{\"function\":\"CreateEvidence\",\"Args\":[\"{\\\"evidence_id\\\":\\\"${SMOKE_ID}\\\",\\\"evidence_type\\\":\\\"DEPLOY_SMOKE_TEST\\\",\\\"evidence_hash\\\":\\\"sha256:0000\\\"}\"]}"

sleep 2
peer_exec "${NETGUARDORG_MSP}" NetGuardOrgMSP peer0.netguardorg.netguard.local:7051 \
  peer chaincode query -C "${CHANNEL_NAME}" -n "${CC_NAME}" \
    -c "{\"function\":\"GetEvidence\",\"Args\":[\"${SMOKE_ID}\"]}"

echo "== netguard-evidence chaincode is deployed and callable on ${CHANNEL_NAME} =="
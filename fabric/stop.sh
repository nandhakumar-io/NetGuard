#!/usr/bin/env bash
# stop.sh — tear down the dev Fabric network started by start.sh.
#
# Usage:
#   ./stop.sh          stop and remove containers, keep ledger volumes
#   ./stop.sh --purge  also remove ledger volumes and generated crypto
#                       material/channel artifacts (full reset -- next
#                       run must start from ./bootstrap.sh again)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FABRIC_ROOT="$(cd "${HERE}/.." && pwd)"
NETWORK_DIR="${FABRIC_ROOT}/network"

echo "== stopping Fabric dev network =="
(cd "${NETWORK_DIR}" && docker compose down)

if [[ "${1:-}" == "--purge" ]]; then
  echo "-- purging ledger volumes"
  (cd "${NETWORK_DIR}" && docker compose down --volumes)
  echo "-- removing generated crypto material and channel artifacts"
  rm -rf "${FABRIC_ROOT}/organizations" "${NETWORK_DIR}/channel-artifacts"
  echo "== purged. Run ./bootstrap.sh before the next ./start.sh =="
else
  echo "== stopped. Ledger volumes preserved -- ./start.sh resumes from the existing chain. Use --purge for a full reset. =="
fi
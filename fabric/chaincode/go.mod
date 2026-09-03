module github.com/netguard/fabric/chaincode/netguard-evidence

// Pinned to the same Fabric line as fabric/scripts/bootstrap.sh's
// FABRIC_VERSION (2.5.9) and fabric/network/docker-compose.yaml's peer
// images (spec Section 6: "Pin compatible Fabric versions. Do not use
// latest.").
go 1.21

require (
	github.com/hyperledger/fabric-contract-api-go v1.2.2
)
package main

import (
	"log"

	"github.com/hyperledger/fabric-contract-api-go/contractapi"
	chaincode "github.com/netguard/fabric/chaincode/netguard-evidence/chaincode"
)

func main() {
	evidenceChaincode, err := contractapi.NewChaincode(&chaincode.EvidenceContract{})
	if err != nil {
		log.Panicf("error creating netguard-evidence chaincode: %v", err)
	}
	if err := evidenceChaincode.Start(); err != nil {
		log.Panicf("error starting netguard-evidence chaincode: %v", err)
	}
}
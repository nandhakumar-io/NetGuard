// Package chaincode implements the netguard-evidence smart contract
// (spec Section 7). It is the on-chain half of the "off-chain evidence +
// on-chain hash" split: every field here is one of the non-sensitive
// identifiers listed in Section 2 -- never a full evidence body, never a
// secret. The shape of EvidenceRecord below is intentionally identical
// to the `ledger_record` dict built by
// backend/app/services/fabric_service.py:submit_pending() and sent by
// backend/app/services/fabric_gateway_client.py:submit_evidence(), so the
// fabric-gateway sidecar can pass that JSON straight through to
// CreateEvidence without reshaping it.
package chaincode

import (
	"encoding/json"
	"fmt"

	"github.com/hyperledger/fabric-contract-api-go/contractapi"
)

// compositeKeyType indexes evidence by change_request_id so
// GetEvidenceByChangeRequest doesn't require a CouchDB rich query (and
// therefore also works against the LevelDB state database, not just the
// CouchDB instances configured in fabric/network/docker-compose.yaml).
const compositeKeyType = "cr~evidence"

// EvidenceRecord is the on-chain record for one evidence anchor.
// Fields mirror Section 7's example ledger record plus previous_evidence_id
// (Section 15's evidence-chain link) and the two ledger-assigned fields
// (TxID/TxTimestamp) that let a verifier cite exactly which Fabric
// transaction anchored this record without a separate history lookup.
type EvidenceRecord struct {
	EvidenceID              string `json:"evidence_id"`
	EvidenceType            string `json:"evidence_type"`
	ChangeRequestID         string `json:"change_request_id,omitempty"`
	DeviceID                string `json:"device_id,omitempty"`
	EvidenceHash            string `json:"evidence_hash"`
	ConfigurationHash       string `json:"configuration_hash,omitempty"`
	Result                  string `json:"result,omitempty"`
	PolicyVersion           string `json:"policy_version,omitempty"`
	BatfishVersion          string `json:"batfish_version,omitempty"`
	ValidationEngineVersion string `json:"validation_engine_version,omitempty"`
	Timestamp               string `json:"timestamp,omitempty"`
	ActorSubject            string `json:"actor_subject,omitempty"`
	PreviousEvidenceID      string `json:"previous_evidence_id,omitempty"`

	// Ledger-assigned at CreateEvidence time -- never set by the caller.
	TxID        string `json:"tx_id,omitempty"`
	TxTimestamp string `json:"tx_timestamp,omitempty"`
}

// HistoryEntry wraps one GetHistoryForKey result. IsDelete is always
// false in practice -- this chaincode never deletes evidence keys
// (Section 7: "Do not implement destructive updates that erase
// historical evidence") -- but is surfaced anyway since it's part of
// the underlying Fabric API and a genuinely-absent value would be a
// more surprising thing to silently drop than to report.
type HistoryEntry struct {
	TxID      string          `json:"tx_id"`
	Timestamp string          `json:"timestamp"`
	IsDelete  bool            `json:"is_delete"`
	Value     *EvidenceRecord `json:"value,omitempty"`
}

// VerificationResult is returned by VerifyEvidence. Note this is a
// *ledger-side* convenience check (does the caller's expected_hash match
// what's on-chain for this evidence_id) -- it is not a substitute for
// backend/app/services/fabric_service.py:verify_evidence()'s recomputation
// of the hash from the off-chain evidence body; that recomputation must
// still happen off-chain, since the chaincode never sees the full
// evidence body to hash in the first place (Section 2).
type VerificationResult struct {
	EvidenceID string `json:"evidence_id"`
	Verified   bool   `json:"verified"`
	LedgerHash string `json:"ledger_hash"`
}

// EvidenceContract implements the netguard-evidence chaincode.
type EvidenceContract struct {
	contractapi.Contract
}

// CreateEvidence anchors one evidence record. Idempotent on evidence_id
// (Section 19): a second CreateEvidence call for an evidence_id that is
// already on the ledger with an *identical* evidence_hash is a no-op that
// returns the existing record (covers NATS/Celery redelivery and
// anchor-task retries after a successful-but-unacknowledged first
// submission). A second call with a *different* evidence_hash for the
// same evidence_id is rejected -- that is not a retry, it's an attempt to
// overwrite prior evidence, which Section 7 explicitly disallows
// ("Evidence must be effectively append-only. If evidence changes,
// create a new evidence record/version" via previous_evidence_id,
// handled entirely off-chain in evidence_service.build_evidence).
func (c *EvidenceContract) CreateEvidence(ctx contractapi.TransactionContextInterface, evidenceJSON string) (*EvidenceRecord, error) {
	var record EvidenceRecord
	if err := json.Unmarshal([]byte(evidenceJSON), &record); err != nil {
		return nil, fmt.Errorf("invalid evidence JSON: %w", err)
	}
	if record.EvidenceID == "" {
		return nil, fmt.Errorf("evidence_id is required")
	}
	if record.EvidenceHash == "" {
		return nil, fmt.Errorf("evidence_hash is required")
	}

	existingBytes, err := ctx.GetStub().GetState(record.EvidenceID)
	if err != nil {
		return nil, fmt.Errorf("failed to read state for %s: %w", record.EvidenceID, err)
	}
	if existingBytes != nil {
		var existing EvidenceRecord
		if err := json.Unmarshal(existingBytes, &existing); err != nil {
			return nil, fmt.Errorf("existing ledger record for %s is corrupt: %w", record.EvidenceID, err)
		}
		if existing.EvidenceHash == record.EvidenceHash {
			// Idempotent retry -- return what's already on the ledger,
			// no new write, no new tx.
			return &existing, nil
		}
		return nil, fmt.Errorf(
			"evidence %s already anchored with a different hash (ledger=%s, submitted=%s); "+
				"evidence is append-only, submit a new evidence_id referencing this one via previous_evidence_id instead",
			record.EvidenceID, existing.EvidenceHash, record.EvidenceHash,
		)
	}

	txTimestamp, err := ctx.GetStub().GetTxTimestamp()
	if err != nil {
		return nil, fmt.Errorf("failed to read tx timestamp: %w", err)
	}
	record.TxID = ctx.GetStub().GetTxID()
	record.TxTimestamp = txTimestamp.AsTime().UTC().Format("2006-01-02T15:04:05.000Z")

	recordBytes, err := json.Marshal(record)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal evidence record: %w", err)
	}
	if err := ctx.GetStub().PutState(record.EvidenceID, recordBytes); err != nil {
		return nil, fmt.Errorf("failed to write evidence %s: %w", record.EvidenceID, err)
	}

	if record.ChangeRequestID != "" {
		compositeKey, err := ctx.GetStub().CreateCompositeKey(compositeKeyType, []string{record.ChangeRequestID, record.EvidenceID})
		if err != nil {
			return nil, fmt.Errorf("failed to build change-request index key: %w", err)
		}
		if err := ctx.GetStub().PutState(compositeKey, []byte{0x00}); err != nil {
			return nil, fmt.Errorf("failed to write change-request index for %s: %w", record.EvidenceID, err)
		}
	}

	return &record, nil
}

// GetEvidence returns the current ledger record for evidence_id, or an
// error if it has never been anchored. The fabric-gateway sidecar
// translates a "not found" error from this function into an HTTP 404
// for GET /evidence/{id} (see fabric_gateway_client.get_evidence).
func (c *EvidenceContract) GetEvidence(ctx contractapi.TransactionContextInterface, evidenceID string) (*EvidenceRecord, error) {
	return c.readEvidence(ctx, evidenceID)
}

func (c *EvidenceContract) readEvidence(ctx contractapi.TransactionContextInterface, evidenceID string) (*EvidenceRecord, error) {
	recordBytes, err := ctx.GetStub().GetState(evidenceID)
	if err != nil {
		return nil, fmt.Errorf("failed to read state for %s: %w", evidenceID, err)
	}
	if recordBytes == nil {
		return nil, fmt.Errorf("evidence %s not found", evidenceID)
	}
	var record EvidenceRecord
	if err := json.Unmarshal(recordBytes, &record); err != nil {
		return nil, fmt.Errorf("ledger record for %s is corrupt: %w", evidenceID, err)
	}
	return &record, nil
}

// VerifyEvidence compares expectedHash (normally the hash the caller
// just recomputed off-chain) against this evidence_id's on-ledger
// evidence_hash. See VerificationResult's doc comment -- this is a
// convenience check, not the authoritative verification path.
func (c *EvidenceContract) VerifyEvidence(ctx contractapi.TransactionContextInterface, evidenceID string, expectedHash string) (*VerificationResult, error) {
	record, err := c.readEvidence(ctx, evidenceID)
	if err != nil {
		return nil, err
	}
	return &VerificationResult{
		EvidenceID: evidenceID,
		Verified:   record.EvidenceHash == expectedHash,
		LedgerHash: record.EvidenceHash,
	}, nil
}

// GetEvidenceHistory returns every ledger write ever made to this
// evidence_id's key, oldest first. Because CreateEvidence never
// overwrites an existing key with a different hash (see above), this
// will normally contain exactly one entry -- but the function still
// walks the real key history rather than assuming that, since a chain
// upgrade or a future chaincode version bump could legitimately touch
// the same key more than once.
func (c *EvidenceContract) GetEvidenceHistory(ctx contractapi.TransactionContextInterface, evidenceID string) ([]*HistoryEntry, error) {
	iterator, err := ctx.GetStub().GetHistoryForKey(evidenceID)
	if err != nil {
		return nil, fmt.Errorf("failed to read history for %s: %w", evidenceID, err)
	}
	defer iterator.Close()

	var entries []*HistoryEntry
	for iterator.HasNext() {
		mod, err := iterator.Next()
		if err != nil {
			return nil, fmt.Errorf("failed to iterate history for %s: %w", evidenceID, err)
		}
		entry := &HistoryEntry{
			TxID:      mod.GetTxId(),
			Timestamp: mod.GetTimestamp().AsTime().UTC().Format("2006-01-02T15:04:05.000Z"),
			IsDelete:  mod.GetIsDelete(),
		}
		if !mod.GetIsDelete() && len(mod.GetValue()) > 0 {
			var record EvidenceRecord
			if err := json.Unmarshal(mod.GetValue(), &record); err != nil {
				return nil, fmt.Errorf("history entry for %s is corrupt: %w", evidenceID, err)
			}
			entry.Value = &record
		}
		entries = append(entries, entry)
	}

	// GetHistoryForKey returns newest-first; reverse so callers (and
	// fabric_service.get_evidence_history's zip against the local
	// revision chain, which is oldest-first) see it chronologically.
	for i, j := 0, len(entries)-1; i < j; i, j = i+1, j-1 {
		entries[i], entries[j] = entries[j], entries[i]
	}
	return entries, nil
}

// GetEvidenceByChangeRequest returns every evidence record anchored for
// a given change_request_id, via the cr~evidence composite-key index
// maintained by CreateEvidence. Order is index-iteration order (Fabric's
// composite-key range order), not creation time -- callers that need
// evidence-chain order should follow previous_evidence_id links off-chain
// instead (Section 15), same as fabric_service.get_evidence_history does.
func (c *EvidenceContract) GetEvidenceByChangeRequest(ctx contractapi.TransactionContextInterface, changeRequestID string) ([]*EvidenceRecord, error) {
	iterator, err := ctx.GetStub().GetStateByPartialCompositeKey(compositeKeyType, []string{changeRequestID})
	if err != nil {
		return nil, fmt.Errorf("failed to query change-request index for %s: %w", changeRequestID, err)
	}
	defer iterator.Close()

	records := []*EvidenceRecord{}
	for iterator.HasNext() {
		item, err := iterator.Next()
		if err != nil {
			return nil, fmt.Errorf("failed to iterate change-request index for %s: %w", changeRequestID, err)
		}
		_, keyParts, err := ctx.GetStub().SplitCompositeKey(item.GetKey())
		if err != nil {
			return nil, fmt.Errorf("failed to parse index key: %w", err)
		}
		if len(keyParts) != 2 {
			continue
		}
		evidenceID := keyParts[1]
		record, err := c.readEvidence(ctx, evidenceID)
		if err != nil {
			return nil, err
		}
		records = append(records, record)
	}
	return records, nil
}
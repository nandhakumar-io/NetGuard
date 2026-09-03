"""Evidence construction/hashing for the Hyperledger Fabric evidence layer.

This module owns exactly the "off-chain evidence + on-chain hash" split
described in the integration spec: it builds the JSON evidence body that
lives in Postgres (app.models.blockchain_evidence.BlockchainEvidence.
evidence_body), canonicalizes it deterministically, and SHA-256 hashes
the canonical form. Only that hash (plus a handful of non-sensitive
identifiers) is ever handed to app.services.fabric_service to anchor.

Nothing in here talks to Fabric, NATS, or the DB -- it is pure
data-shaping + hashing, which is what makes it independently unit
testable (see backend/tests/test_evidence_service.py) without a running
Fabric network.

Canonicalization contract (must never change without bumping
CANONICALIZATION_VERSION, since that would silently invalidate every
previously-anchored hash):

    * json.dumps with sort_keys=True and fixed separators -- key order
      and whitespace never affect the hash.
    * All values are JSON-native (str/int/float/bool/None/list/dict) --
      callers must not pass datetimes, UUIDs, enums, or Decimals
      directly; convert to str/int first (see _normalize).
    * ensure_ascii=True so the byte representation is stable regardless
      of the platform's default encoding.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from typing import Any

HASH_ALGORITHM = "SHA-256"
CANONICALIZATION_VERSION = 1


def _normalize(value: Any) -> Any:
    """Recursively coerce non-JSON-native values to a stable JSON-native
    form so canonicalize_evidence never has to special-case a caller's
    ORM objects. Applied once, at build_evidence() time, rather than
    inside json.dumps(default=...) -- that keeps canonicalize_evidence
    itself trivial to reason about (and to reimplement identically in
    the Fabric Gateway sidecar / chaincode if that's ever needed for an
    independent verifier)."""
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        return value
    # Enums (e.g. AnchorStatus) and anything else with a plain string
    # form -- last resort, keeps build_evidence from ever raising on an
    # unexpected type at the cost of an explicit str().
    return str(value)


def build_evidence(
    evidence_type: str,
    *,
    evidence_id: str,
    change_request_id: str | None = None,
    device_id: str | None = None,
    timestamp: str,
    actor_subject: str | None = None,
    previous_evidence_id: str | None = None,
    previous_evidence_hash: str | None = None,
    application_version: str | None = None,
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the off-chain evidence body for one evidence record.

    `fields` carries the evidence-type-specific payload (OPA decision,
    Batfish result, risk score, deployment id, verification result,
    ...) -- see the spec's per-section JSON examples. Kept as a single
    nested dict rather than flattening every possible field onto this
    function's signature, since the set of relevant fields genuinely
    differs by evidence_type (Section 8) and evidence_service should not
    need to change every time a new evidence-type-specific field is
    added upstream.
    """
    body: dict[str, Any] = {
        "evidence_id": evidence_id,
        "evidence_type": str(evidence_type),
        "hash_algorithm": HASH_ALGORITHM,
        "canonicalization_version": CANONICALIZATION_VERSION,
        "change_request_id": change_request_id,
        "device_id": device_id,
        "actor_subject": actor_subject,
        "timestamp": timestamp,
        "previous_evidence_id": previous_evidence_id,
        "previous_evidence_hash": previous_evidence_hash,
        "application_version": application_version,
    }
    if fields:
        body.update(fields)
    return _normalize(body)


def canonicalize_evidence(evidence_body: dict[str, Any]) -> str:
    """Deterministic canonical JSON serialization -- see module docstring
    for the contract. Never hash a dict directly (e.g. `str(evidence)` or
    Python's `repr`/`hash()`); both are insertion-order- and
    interpreter-dependent and would make two byte-identical evidence
    bodies hash differently across a dict-ordering change or a Python
    version bump."""
    return json.dumps(
        _normalize(evidence_body),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def hash_evidence(evidence_body: dict[str, Any]) -> str:
    """SHA-256 of the canonical form, prefixed "sha256:" (matches the
    `configuration_hash`/`evidence_hash` display format used throughout
    the spec's JSON examples, and disambiguates the hash algorithm if it
    is ever changed for new evidence going forward)."""
    canonical = canonicalize_evidence(evidence_body)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def hash_config(raw_config: str) -> str:
    """Hash of an exact device configuration payload -- used for both
    `configuration_hash` on evidence records and the pre-deployment
    integrity check (Section 11: approved_configuration_hash ==
    deployment_configuration_hash). Deliberately the same algorithm as
    app.services.snapshot_service.compute_checksum (plain hex sha256 of
    the raw text) but "sha256:"-prefixed to match evidence_hash's
    format; snapshot_service's un-prefixed hex checksums are NOT
    reused directly as configuration_hash values without this prefix,
    to keep "a value that was hashed for Fabric evidence" visually
    distinguishable from "a value that was hashed for snapshot dedup"
    even though the underlying bytes hashed are the same."""
    digest = hashlib.sha256(raw_config.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def verify_evidence(evidence_body: dict[str, Any], expected_hash: str) -> tuple[bool, str]:
    """Recompute the hash of `evidence_body` and compare against
    `expected_hash` (normally the hash Fabric has on ledger for this
    evidence_id). Returns (verified, calculated_hash) so callers can
    surface both values even on a mismatch (see the spec's Section 21
    API response shape and Section 24 tamper-detection UI)."""
    calculated = hash_evidence(evidence_body)
    return calculated == expected_hash, calculated

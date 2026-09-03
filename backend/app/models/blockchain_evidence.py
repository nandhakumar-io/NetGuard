"""Hyperledger Fabric evidence-anchoring ledger (off-chain half).

This table is the PostgreSQL side of the "off-chain evidence + on-chain
hash" split described in the Fabric integration spec: the full evidence
JSON body lives here (and, transitively, in whatever it references --
ConfigSnapshot, ChangeRequest.policy_validation_result/batfish_result,
etc.), while Fabric itself only ever receives `evidence_hash` plus a
handful of non-sensitive identifiers (see app.services.evidence_service
and app.services.fabric_service). Never add a column here that duplicates
a secret already covered by credential_service/OpenBao -- this table is
readable by anyone with CR/audit visibility.

Append-only in spirit, same convention as AuditLog: rows are created and
have their anchor_status/fabric_* columns updated as anchoring proceeds,
but the evidence_hash/configuration_hash/evidence_id of a given row are
never mutated after creation. If evidence needs to change, a *new*
BlockchainEvidence row is created that references the old one via
previous_evidence_id (Section 15 of the spec) -- see
evidence_service.build_evidence(previous_evidence_id=...).
"""
import enum
import uuid

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.database import Base


class EvidenceType(str, enum.Enum):
    CHANGE_REQUEST_CREATED = "CHANGE_REQUEST_CREATED"
    CHANGE_VALIDATION = "CHANGE_VALIDATION"
    OPA_DECISION = "OPA_DECISION"
    BATFISH_VALIDATION = "BATFISH_VALIDATION"
    CHANGE_APPROVED = "CHANGE_APPROVED"
    CHANGE_REJECTED = "CHANGE_REJECTED"
    DEPLOYMENT_STARTED = "DEPLOYMENT_STARTED"
    DEPLOYMENT_COMPLETED = "DEPLOYMENT_COMPLETED"
    DEPLOYMENT_FAILED = "DEPLOYMENT_FAILED"
    POST_DEPLOYMENT_VERIFICATION = "POST_DEPLOYMENT_VERIFICATION"
    ROLLBACK_STARTED = "ROLLBACK_STARTED"
    ROLLBACK_COMPLETED = "ROLLBACK_COMPLETED"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    CONFIGURATION_BASELINE = "CONFIGURATION_BASELINE"
    CONFIGURATION_DRIFT = "CONFIGURATION_DRIFT"
    POLICY_VERSION = "POLICY_VERSION"


class AnchorStatus(str, enum.Enum):
    PENDING = "PENDING"
    ANCHORING = "ANCHORING"
    ANCHORED = "ANCHORED"
    FAILED = "FAILED"
    VERIFIED = "VERIFIED"
    MISMATCH = "MISMATCH"


class BlockchainEvidence(Base):
    __tablename__ = "blockchain_evidence"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Human/log-friendly id (e.g. "EV-10042"), independent of the UUID pk so
    # existing NetGuard IDs (CR-xxxx) and Fabric-side logs read consistently.
    # This is the idempotency key used by fabric_service/anchor_evidence_task.
    evidence_id = Column(String(64), nullable=False, unique=True, index=True)
    evidence_type = Column(Enum(EvidenceType), nullable=False, index=True)

    change_request_id = Column(UUID(as_uuid=True), ForeignKey("change_requests.id"), nullable=True, index=True)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True, index=True)
    snapshot_id = Column(UUID(as_uuid=True), ForeignKey("config_snapshots.id"), nullable=True)
    deployment_id = Column(UUID(as_uuid=True), ForeignKey("deployments.id"), nullable=True)

    # Off-chain evidence body actually hashed (see evidence_service). Kept
    # here, NOT on Fabric -- Fabric only ever sees evidence_hash below.
    # JSONB on Postgres for queryability; falls back to JSON elsewhere
    # (sqlite in tests), matching the ChangeRequest.policy_* convention.
    evidence_body = Column(JSON().with_variant(JSONB, "postgresql"), nullable=False)

    evidence_hash = Column(String(71), nullable=False)  # "sha256:" + 64 hex chars
    configuration_hash = Column(String(71), nullable=True)
    hash_algorithm = Column(String(16), nullable=False, default="SHA-256", server_default="SHA-256")
    canonicalization_version = Column(Integer, nullable=False, default=1, server_default="1")

    # Evidence chain (Section 15): the evidence this record supersedes
    # (revision) or the validation evidence an approval/deployment record
    # references (lifecycle link). previous_evidence_hash is denormalized
    # onto this row at write time so a verifier can walk the chain without
    # an extra join even if the referenced row is ever archived.
    previous_evidence_id = Column(String(64), nullable=True, index=True)
    previous_evidence_hash = Column(String(71), nullable=True)

    fabric_channel = Column(String(128), nullable=True)
    fabric_chaincode = Column(String(128), nullable=True)
    fabric_transaction_id = Column(String(128), nullable=True, index=True)
    fabric_block_number = Column(Integer, nullable=True)

    anchor_status = Column(
        Enum(AnchorStatus), nullable=False, default=AnchorStatus.PENDING, server_default=AnchorStatus.PENDING.value
    )
    anchor_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    anchor_error = Column(Text, nullable=True)

    policy_version = Column(String(64), nullable=True)
    batfish_version = Column(String(64), nullable=True)
    validation_engine_version = Column(String(64), nullable=True)
    application_version = Column(String(64), nullable=True)

    actor_subject = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    anchored_at = Column(DateTime(timezone=True), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)

    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True)

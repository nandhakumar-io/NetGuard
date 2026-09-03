"""Fabric anchoring orchestration -- the single entry point the rest of
NetGuard uses to get evidence onto (and verified against) Hyperledger
Fabric.

This module owns:

  * anchor_evidence()   -- create the BlockchainEvidence row + kick off
                            (async, by default) submission to Fabric.
  * get_evidence()       -- read a BlockchainEvidence row by evidence_id.
  * verify_evidence()    -- recompute the off-chain hash and compare
                             against what Fabric has on ledger.
  * get_evidence_history()
  * submit_pending()     -- the actual Fabric Gateway call + idempotent
                             status transition; called by the Celery
                             anchor worker (app.tasks.anchor_evidence_task),
                             not directly by request handlers.

It does NOT decide policy/compliance/deployment outcomes (Section 1: "
Hyperledger Fabric must not make compliance, security, or deployment
decisions") -- callers pass in an already-decided evidence body built by
app.services.evidence_service from the OPA/Batfish/risk/approval/
deployment/verification results those other services already computed.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.blockchain_evidence import (
    AnchorStatus,
    BlockchainEvidence,
    EvidenceType,
)
from app.services import audit_service, event_bus, evidence_service
from app.services.fabric_gateway_client import (
    FabricGatewayError,
    FabricUnconfiguredError,
)

logger = logging.getLogger(__name__)

EVT_ANCHOR_REQUESTED = "netguard.evidence.anchor.requested"
EVT_ANCHOR_COMPLETED = "netguard.evidence.anchor.completed"
EVT_ANCHOR_FAILED = "netguard.evidence.anchor.failed"
EVT_VERIFICATION_REQUESTED = "netguard.evidence.verification.requested"
EVT_VERIFICATION_COMPLETED = "netguard.evidence.verification.completed"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(event_type: str, **payload) -> None:
    # Best-effort UI live-update, same pattern as change_validation_service
    # and event_bus's other publishers -- a NATS hiccup here must never
    # affect the anchoring outcome itself (that's tracked in Postgres).
    event_bus.publish_event(event_type, channel=event_type, **payload)


def is_critical_change(change_request) -> bool:
    """What "critical change" means for FABRIC_REQUIRED_FOR_CRITICAL_CHANGES
    gating (Section 20). Mirrors the dual-approval trigger already used
    elsewhere in the pipeline (ChangeRequest.requires_dual_approval /
    dual_approval_reason) rather than inventing a second definition of
    "critical" -- a change already judged critical enough to need two
    human approvers is exactly the change that should not silently
    deploy with unanchored evidence."""
    return bool(getattr(change_request, "requires_dual_approval", False)) or (
        getattr(change_request, "priority", None) is not None
        and str(getattr(change_request, "priority")).lower() in ("emergency", "changepriority.emergency")
    )


def anchor_evidence(
    db: Session,
    *,
    evidence_type: EvidenceType | str,
    fields: dict,
    change_request=None,
    device=None,
    snapshot_id: uuid.UUID | None = None,
    deployment_id: uuid.UUID | None = None,
    actor_subject: str | None = None,
    previous_evidence: BlockchainEvidence | None = None,
    configuration_hash: str | None = None,
    policy_version: str | None = None,
    batfish_version: str | None = None,
    validation_engine_version: str | None = None,
    tenant_id=None,
) -> BlockchainEvidence:
    """Build + persist one evidence record and request anchoring.

    Always creates the Postgres row (evidence is never lost just because
    Fabric is unreachable -- Section 18/20), and, when FABRIC_ENABLED,
    either submits synchronously (FABRIC_ASYNC_ANCHOR=false) or hands off
    to the Celery anchor worker (the default). When FABRIC_ENABLED is
    false, the row is created and left PENDING permanently -- useful for
    dev/local environments that want the audit trail without standing up
    a Fabric network.
    """
    et = EvidenceType(evidence_type) if not isinstance(evidence_type, EvidenceType) else evidence_type
    cr_id = getattr(change_request, "id", None)
    device_id = getattr(device, "id", None) if device is not None else None
    evidence_id = f"EV-{uuid.uuid4().hex[:10].upper()}"

    body = evidence_service.build_evidence(
        et.value,
        evidence_id=evidence_id,
        change_request_id=str(cr_id) if cr_id else None,
        device_id=str(device_id) if device_id else None,
        timestamp=_now_iso(),
        actor_subject=actor_subject,
        previous_evidence_id=previous_evidence.evidence_id if previous_evidence else None,
        previous_evidence_hash=previous_evidence.evidence_hash if previous_evidence else None,
        application_version=settings.APP_NAME,
        fields=fields,
    )
    evidence_hash = evidence_service.hash_evidence(body)

    record = BlockchainEvidence(
        evidence_id=evidence_id,
        evidence_type=et,
        change_request_id=cr_id,
        device_id=device_id,
        snapshot_id=snapshot_id,
        deployment_id=deployment_id,
        evidence_body=body,
        evidence_hash=evidence_hash,
        configuration_hash=configuration_hash,
        hash_algorithm=evidence_service.HASH_ALGORITHM,
        canonicalization_version=evidence_service.CANONICALIZATION_VERSION,
        previous_evidence_id=previous_evidence.evidence_id if previous_evidence else None,
        previous_evidence_hash=previous_evidence.evidence_hash if previous_evidence else None,
        fabric_channel=settings.FABRIC_CHANNEL,
        fabric_chaincode=settings.FABRIC_CHAINCODE,
        anchor_status=AnchorStatus.PENDING,
        policy_version=policy_version,
        batfish_version=batfish_version,
        validation_engine_version=validation_engine_version,
        application_version=settings.APP_NAME,
        actor_subject=actor_subject,
        tenant_id=tenant_id,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    audit_service.record_event(
        db, actor=actor_subject or "system", action="EVIDENCE_CREATED",
        result="Success", device_hostname=getattr(device, "hostname", None),
        change_request_id=cr_id,
        detail=f"[evidence_id={evidence_id}] type={et.value} hash={evidence_hash}",
        tenant_id=tenant_id,
    )
    _emit("netguard.evidence.created", evidence_id=evidence_id, evidence_type=et.value,
          change_request_id=str(cr_id) if cr_id else None)

    if not settings.FABRIC_ENABLED:
        return record

    if settings.FABRIC_ASYNC_ANCHOR:
        _dispatch_anchor_task(record.evidence_id)
    else:
        submit_pending(db, record)
        db.refresh(record)

    return record


def _dispatch_anchor_task(evidence_id: str) -> None:
    # Imported lazily to avoid a circular import (app.tasks imports
    # several services at module scope; fabric_service must not be one
    # of the things app.tasks imports before app.celery_app exists).
    from app.tasks import anchor_evidence_task

    anchor_evidence_task.delay(evidence_id)


def submit_pending(db: Session, record: BlockchainEvidence) -> BlockchainEvidence:
    """Submit one PENDING/FAILED evidence record to Fabric and update its
    status. Idempotent: uses evidence_id as the logical key (Section 19)
    -- if the chaincode already has this evidence_id (e.g. a NATS/Celery
    redelivery after a successful-but-unacknowledged first attempt), the
    sidecar/chaincode returns the existing transaction rather than a new
    one, and this function treats that identically to a fresh success.

    Called by app.tasks.anchor_evidence_task (the normal path) and
    directly by anchor_evidence() when FABRIC_ASYNC_ANCHOR=false.
    """
    if record.anchor_status == AnchorStatus.ANCHORED:
        return record  # already done -- nothing to do, safe no-op retry

    record.anchor_status = AnchorStatus.ANCHORING
    record.anchor_attempts = (record.anchor_attempts or 0) + 1
    db.commit()

    ledger_record = {
        "evidence_id": record.evidence_id,
        "evidence_type": record.evidence_type.value,
        "change_request_id": str(record.change_request_id) if record.change_request_id else None,
        "device_id": str(record.device_id) if record.device_id else None,
        "evidence_hash": record.evidence_hash,
        "configuration_hash": record.configuration_hash,
        "result": record.evidence_body.get("result") or record.evidence_body.get("decision"),
        "policy_version": record.policy_version,
        "batfish_version": record.batfish_version,
        "validation_engine_version": record.validation_engine_version,
        "timestamp": record.evidence_body.get("timestamp"),
        "actor_subject": record.actor_subject,
        "previous_evidence_id": record.previous_evidence_id,
    }

    from app.services import fabric_gateway_client
    from app.services.fabric_gateway_client import FabricDuplicateError

    try:
        result = fabric_gateway_client.submit_evidence(ledger_record)
    except FabricDuplicateError:
        # Section 19 idempotency: sidecar returned 409 — the evidence_id is already
        # on the ledger (e.g. a Celery redelivery after a successful but
        # unacknowledged first attempt). Treat it as a success by reading back
        # the existing ledger record so we can persist the correct tx/block.
        logger.info(
            "submit_pending: evidence_id=%s already on ledger (409 duplicate) — "
            "treating as idempotent success", record.evidence_id,
        )
        try:
            existing = fabric_gateway_client.get_evidence(record.evidence_id)
        except Exception:  # noqa: BLE001
            existing = None
        result = existing or {}
    except FabricUnconfiguredError as exc:
        # Misconfiguration — permanent, don't retry, surface immediately.
        record.anchor_status = AnchorStatus.FAILED
        record.anchor_error = str(exc)[:2000]
        db.commit()
        audit_service.record_event(
            db, actor="system", action="EVIDENCE_ANCHOR_FAILED",
            result="Failed", change_request_id=record.change_request_id,
            detail=f"[evidence_id={record.evidence_id}] UNCONFIGURED: {exc}",
        )
        _emit(EVT_ANCHOR_FAILED, evidence_id=record.evidence_id, error=str(exc)[:500],
              attempts=record.anchor_attempts, status=AnchorStatus.FAILED.value)
        raise
    except FabricGatewayError as exc:
        # Transient error — keep PENDING (or flip to FAILED once retries exhausted).
        record.anchor_status = (
            AnchorStatus.PENDING if record.anchor_attempts < settings.FABRIC_MAX_RETRIES
            else AnchorStatus.FAILED
        )
        record.anchor_error = str(exc)[:2000]
        db.commit()
        audit_service.record_event(
            db, actor="system", action="FABRIC_UNAVAILABLE",
            result="Failed", change_request_id=record.change_request_id,
            detail=f"[evidence_id={record.evidence_id}] attempt={record.anchor_attempts} error={exc}",
        )
        _emit(EVT_ANCHOR_FAILED, evidence_id=record.evidence_id, error=str(exc)[:500],
              attempts=record.anchor_attempts, status=record.anchor_status.value)
        raise

    record.fabric_transaction_id = result.get("transaction_id")
    record.fabric_block_number = result.get("block_number")
    record.anchor_status = AnchorStatus.ANCHORED
    record.anchored_at = datetime.now(timezone.utc)
    record.anchor_error = None
    db.commit()
    db.refresh(record)

    audit_service.record_event(
        db, actor="system", action="EVIDENCE_ANCHORED", result="Success",
        change_request_id=record.change_request_id,
        detail=f"[evidence_id={record.evidence_id}] tx={record.fabric_transaction_id} block={record.fabric_block_number}",
    )
    _emit(EVT_ANCHOR_COMPLETED, evidence_id=record.evidence_id,
          transaction_id=record.fabric_transaction_id, block_number=record.fabric_block_number)
    return record


def get_evidence(db: Session, evidence_id: str) -> BlockchainEvidence | None:
    return db.query(BlockchainEvidence).filter(BlockchainEvidence.evidence_id == evidence_id).first()


def get_evidence_for_change_request(db: Session, change_request_id) -> list[BlockchainEvidence]:
    return (
        db.query(BlockchainEvidence)
        .filter(BlockchainEvidence.change_request_id == change_request_id)
        .order_by(BlockchainEvidence.created_at.asc())
        .all()
    )


def get_evidence_history(db: Session, evidence_id: str) -> list[dict]:
    """Local revision chain (previous_evidence_id links) plus, when
    Fabric is enabled, the ledger's own GetEvidenceHistory for the
    anchored record itself (block-level write history, not application
    revisions -- the two are complementary, not duplicates)."""
    chain: list[BlockchainEvidence] = []
    current = get_evidence(db, evidence_id)
    seen = set()
    while current and current.evidence_id not in seen:
        seen.add(current.evidence_id)
        chain.append(current)
        current = get_evidence(db, current.previous_evidence_id) if current.previous_evidence_id else None
    chain.reverse()
    result = [
        {
            "evidence_id": e.evidence_id,
            "evidence_type": e.evidence_type.value,
            "evidence_hash": e.evidence_hash,
            "anchor_status": e.anchor_status.value,
            "fabric_transaction_id": e.fabric_transaction_id,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in chain
    ]
    if settings.FABRIC_ENABLED:
        from app.services import fabric_gateway_client
        try:
            ledger_history = fabric_gateway_client.get_evidence_history(evidence_id)
        except (FabricGatewayError, FabricUnconfiguredError) as exc:
            logger.warning("get_evidence_history: fabric-gateway unavailable for %s: %s", evidence_id, exc)
            ledger_history = None
        if ledger_history:
            for row, ledger_row in zip(result[-len(ledger_history):], ledger_history):
                row["ledger"] = ledger_row
    return result


def verify_evidence(db: Session, evidence_id: str) -> dict:
    """Recompute the off-chain hash and compare against the ledger's
    hash for this evidence_id (Section 21). Never trusts
    BlockchainEvidence.evidence_hash alone -- that column could itself
    have been tampered with in Postgres, which is exactly the scenario
    the tamper-detection demo (Section 24) exercises; the source of
    truth for "what was the hash at anchor time" is the ledger, not this
    row."""
    record = get_evidence(db, evidence_id)
    if record is None:
        return {"verified": False, "evidence_id": evidence_id, "status": "NOT_FOUND"}

    _emit(EVT_VERIFICATION_REQUESTED, evidence_id=evidence_id)

    calculated_hash = evidence_service.hash_evidence(record.evidence_body)

    ledger_hash = None
    if settings.FABRIC_ENABLED and record.fabric_transaction_id:
        from app.services import fabric_gateway_client
        try:
            ledger = fabric_gateway_client.get_evidence(evidence_id)
        except (FabricGatewayError, FabricUnconfiguredError) as exc:
            record.anchor_error = f"verify: fabric-gateway unavailable: {exc}"
            db.commit()
            return {
                "verified": False, "evidence_id": evidence_id, "status": "FABRIC_UNAVAILABLE",
                "calculated_hash": calculated_hash, "ledger_hash": None,
                "transaction_id": record.fabric_transaction_id, "block_number": record.fabric_block_number,
            }
        ledger_hash = ledger.get("evidence_hash") if ledger else None
    else:
        # Fabric disabled/never anchored -- fall back to comparing against
        # this row's own stored hash, which is strictly weaker (doesn't
        # protect against a compromised DB) but still catches accidental
        # corruption and keeps /verify usable in a dev environment.
        ledger_hash = record.evidence_hash

    verified = ledger_hash is not None and calculated_hash == ledger_hash
    record.verified_at = datetime.now(timezone.utc)
    record.anchor_status = AnchorStatus.VERIFIED if verified else AnchorStatus.MISMATCH
    db.commit()

    if not verified:
        audit_service.record_event(
            db, actor="system", action="EVIDENCE_INTEGRITY_FAILURE" if record.fabric_transaction_id else "EVIDENCE_MISMATCH",
            result="Failed", change_request_id=record.change_request_id,
            detail=f"[evidence_id={evidence_id}] ledger_hash={ledger_hash} calculated_hash={calculated_hash}",
        )
    else:
        audit_service.record_event(
            db, actor="system", action="EVIDENCE_VERIFIED", result="Success",
            change_request_id=record.change_request_id, detail=f"[evidence_id={evidence_id}]",
        )
    _emit(EVT_VERIFICATION_COMPLETED, evidence_id=evidence_id, verified=verified)

    return {
        "verified": verified,
        "evidence_id": evidence_id,
        "calculated_hash": calculated_hash,
        "ledger_hash": ledger_hash,
        "transaction_id": record.fabric_transaction_id,
        "block_number": record.fabric_block_number,
        "status": record.anchor_status.value,
    }


def check_configuration_integrity(approved_hash: str | None, deployment_config: str) -> tuple[bool, str]:
    """Section 11's mandatory pre-deployment gate: approved_configuration_hash
    == deployment_configuration_hash. Returns (ok, deployment_hash) so the
    caller can log/anchor the deployment_hash regardless of outcome. `ok`
    is False (never raises) so pipeline_service can generate the
    APPROVED_CONFIGURATION_MISMATCH evidence/audit event itself with full
    context before blocking, rather than catching an exception."""
    deployment_hash = evidence_service.hash_config(deployment_config)
    if approved_hash is None:
        # No prior approved hash on record -- cannot assert a match, but
        # this is a caller bug (evidence wasn't anchored at approval
        # time), not a tamper signal, so callers should treat this
        # distinctly (log/alert) rather than silently deploying.
        return False, deployment_hash
    return approved_hash == deployment_hash, deployment_hash

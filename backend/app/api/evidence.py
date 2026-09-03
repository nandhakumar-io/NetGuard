"""Evidence verification API (spec Section 21).

Read-only surface over app.services.fabric_service -- this router never
talks to Fabric directly and never decides anything; it just exposes
get_evidence()/verify_evidence()/get_evidence_history()/
get_evidence_for_change_request() over HTTP for the frontend's
Validation/Evidence panel (Section 22/23) and for external audit tooling.

Endpoints:
    GET  /api/v1/evidence/{evidence_id}
    POST /api/v1/evidence/{evidence_id}/verify
    GET  /api/v1/change-requests/{id}/evidence
    GET  /api/v1/evidence/{evidence_id}/history

Every route requires an authenticated user (Keycloak-issued JWT, same as
the rest of NetGuard) and is tenant-scoped where the underlying evidence
carries a tenant_id -- Fabric evidence is audit data, not something to
expose without auth just because the payload itself contains no secrets.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, get_tenant_scope
from app.services import fabric_service

router = APIRouter(prefix="/evidence", tags=["evidence"])
cr_router = APIRouter(prefix="/change-requests", tags=["evidence"])


def _serialize(record, *, include_body: bool = False) -> dict:
    """Serialise a BlockchainEvidence row for the API response.

    ``include_body=False`` (the default) omits ``evidence_body`` from the
    response.  This prevents inadvertent bulk exfiltration of the structured
    evidence payloads — which contain OPA policy decisions, Batfish topology
    findings, and configuration hashes — on list endpoints.  Individual record
    retrieval (GET /evidence/{id}) passes ``include_body=True`` because
    operators explicitly requested that specific record's details.
    """
    out = {
        "evidence_id": record.evidence_id,
        "evidence_type": record.evidence_type.value,
        "change_request_id": str(record.change_request_id) if record.change_request_id else None,
        "device_id": str(record.device_id) if record.device_id else None,
        "deployment_id": str(record.deployment_id) if record.deployment_id else None,
        "evidence_hash": record.evidence_hash,
        "configuration_hash": record.configuration_hash,
        "hash_algorithm": record.hash_algorithm,
        "canonicalization_version": record.canonicalization_version,
        "previous_evidence_id": record.previous_evidence_id,
        "previous_evidence_hash": record.previous_evidence_hash,
        "fabric_channel": record.fabric_channel,
        "fabric_chaincode": record.fabric_chaincode,
        "fabric_transaction_id": record.fabric_transaction_id,
        "fabric_block_number": record.fabric_block_number,
        "anchor_status": record.anchor_status.value,
        "anchor_attempts": record.anchor_attempts,
        "anchor_error": record.anchor_error,
        "policy_version": record.policy_version,
        "batfish_version": record.batfish_version,
        "validation_engine_version": record.validation_engine_version,
        "application_version": record.application_version,
        "actor_subject": record.actor_subject,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "anchored_at": record.anchored_at.isoformat() if record.anchored_at else None,
        "verified_at": record.verified_at.isoformat() if record.verified_at else None,
    }
    if include_body:
        out["evidence_body"] = record.evidence_body
    return out



def _get_or_404(db: Session, evidence_id: str, tenant_id):
    record = fabric_service.get_evidence(db, evidence_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Evidence {evidence_id} not found")
    if tenant_id is not None and record.tenant_id is not None and record.tenant_id != tenant_id:
        # Same "not found" (not 403) as the rest of NetGuard's tenant-scoped
        # lookups, to avoid confirming another tenant's evidence_id exists.
        raise HTTPException(status_code=404, detail=f"Evidence {evidence_id} not found")
    return record


@router.get("/{evidence_id}")
def get_evidence(
    evidence_id: str,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
    tenant_id=Depends(get_tenant_scope),
):
    """Off-chain evidence record + current anchor status. Does NOT
    re-verify against the ledger on every call -- use POST .../verify
    for that (verification is a deliberate, auditable action, not a
    side effect of reading)."""
    return _serialize(_get_or_404(db, evidence_id, tenant_id), include_body=True)


@router.post("/{evidence_id}/verify")
def verify_evidence(
    evidence_id: str,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
    tenant_id=Depends(get_tenant_scope),
):
    """Recompute the off-chain hash and compare against Fabric's ledger
    hash for this evidence_id (Section 21). This is the endpoint the
    frontend's "VERIFY EVIDENCE" button (Section 22) and the
    tamper-detection demo (Section 24) both call."""
    _get_or_404(db, evidence_id, tenant_id)  # 404 + tenant check before doing any work
    return fabric_service.verify_evidence(db, evidence_id)


@router.get("/{evidence_id}/history")
def get_evidence_history(
    evidence_id: str,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
    tenant_id=Depends(get_tenant_scope),
):
    """Local revision chain (previous_evidence_id links) plus, when
    Fabric is enabled, the ledger's own per-record write history."""
    _get_or_404(db, evidence_id, tenant_id)
    return fabric_service.get_evidence_history(db, evidence_id)


@cr_router.get("/{change_request_id}/evidence")
def get_evidence_for_change_request(
    change_request_id: uuid.UUID,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
    tenant_id=Depends(get_tenant_scope),
):
    """Full evidence lifecycle for one change request (Section 15):
    validation, approval, deployment, verification, and any rollback
    evidence anchored against it, in creation order -- what the
    Change Request page's EVIDENCE panel (Section 22/23) renders."""
    records = fabric_service.get_evidence_for_change_request(db, change_request_id)
    if tenant_id is not None:
        records = [r for r in records if r.tenant_id is None or r.tenant_id == tenant_id]
    return [_serialize(r) for r in records]

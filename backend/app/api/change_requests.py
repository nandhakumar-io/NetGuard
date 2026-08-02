import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.change_request import ChangeRequest, ChangeStatus
from app.models.device import Device
from app.models.snapshot import ConfigSnapshot
from app.models.user import User, UserRole
from app.schemas.change_request import ChangeRequestCreate, ChangeRequestRead, RiskAnalysisResult
from app.services import diff_engine, event_bus, protocol_manager, risk_engine, snapshot_service, validation_engine, audit_service
from app.tasks import run_deployment_pipeline_task

router = APIRouter(prefix="/change-requests", tags=["change-requests"])

# Roles permitted to approve/reject change requests (FR-1 RBAC)
APPROVER_ROLES = (UserRole.NETWORK_ADMIN,)


def _latest_config(db: Session, device_id) -> str | None:
    """Best-effort decrypted running-config from the most recent snapshot
    for a device, used as the "current" side of risk analysis. Returns
    None (not an error) if no snapshot exists yet -- analysis still runs,
    just without before/after-aware checks for that device."""
    snap = (
        db.query(ConfigSnapshot)
        .filter(ConfigSnapshot.device_id == device_id)
        .order_by(ConfigSnapshot.seq.desc())
        .first()
    )
    if not snap:
        return None
    try:
        return snapshot_service.decrypt_config(snap.running_config_encrypted)
    except Exception:
        return None


def _fleet_configs(db: Session, exclude_device_id) -> dict[str, str]:
    """{hostname: latest decrypted config} for every other device, used for
    cross-device duplicate-IP / VLAN-conflict checks (SRS 6.2 / FR-6). Kept
    to one query + one snapshot lookup per device rather than N+1 per call
    site; fine at prototype fleet sizes, revisit if this becomes hot."""
    configs: dict[str, str] = {}
    for device in db.query(Device).filter(Device.id != exclude_device_id).all():
        cfg = _latest_config(db, device.id)
        if cfg:
            configs[device.hostname] = cfg
    return configs


@router.get("", response_model=list[ChangeRequestRead])
def list_change_requests(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(ChangeRequest).order_by(ChangeRequest.created_at.desc()).all()


@router.post("", response_model=ChangeRequestRead, status_code=201)
def create_change_request(
    payload: ChangeRequestCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    device = db.get(Device, payload.device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    additional_device_ids_json = None
    if payload.additional_device_ids:
        for extra_id in payload.additional_device_ids:
            if not db.get(Device, extra_id):
                raise HTTPException(status_code=404, detail=f"Device {extra_id} not found")
        import json
        additional_device_ids_json = json.dumps([str(i) for i in payload.additional_device_ids])

    # Current config comes from the device's most recent snapshot (best
    # effort -- analysis still runs without it, just skipping the
    # before/after-aware checks). Every *other* device's latest config is
    # also pulled so the analyzer can catch fleet-wide conflicts (duplicate
    # IPs, VLAN naming conflicts) rather than only within this one device.
    # Current config: prefer a fresh live read from the device itself (it's
    # already been proven reachable -- see health_monitor/snmp -- so use
    # that reachability instead of trusting a snapshot that may be stale or
    # may not exist yet for a device that's never been deployed to). Falls
    # back to the last snapshot on file if the live read fails for any
    # reason (device briefly unreachable, no supported protocol configured,
    # etc.) -- never blocks change-request submission on this being a
    # best-effort improvement, not a hard requirement.
    pm = protocol_manager.ProtocolManager(db, device, operator=current_user.email)
    live_running = pm.get_running_config()
    current_config = live_running.output if live_running.success else _latest_config(db, payload.device_id)
    fleet_configs = _fleet_configs(db, payload.device_id)

    diff_text = diff_engine.generate_diff(current_config, payload.proposed_config)
    validation = validation_engine.validate_syntax(
        payload.proposed_config,
        vendor=device.vendor.value if hasattr(device.vendor, "value") else device.vendor,
        current_config=current_config,
    )
    risk: RiskAnalysisResult = risk_engine.analyze(payload.proposed_config, current_config, fleet_configs)
    critical = risk_engine.is_critical(risk)

    # Blast-radius dual approval (SRS 6.2 / FR-6 extension): a change fanned
    # out to enough devices requires two distinct Network Administrator
    # approvals regardless of its individual risk score -- a low-risk
    # change pushed to 50 devices is still high blast-radius.
    device_count = 1 + len(payload.additional_device_ids or [])
    blast_radius_triggered = device_count > settings.RISK_BLAST_RADIUS_DUAL_APPROVAL_THRESHOLD

    critical_triggered = critical and settings.RISK_CRITICAL_DUAL_APPROVAL_ENABLED
    requires_dual_approval = critical_triggered or blast_radius_triggered
    dual_approval_reason = None
    if critical_triggered and blast_radius_triggered:
        dual_approval_reason = "Critical Risk + Blast Radius"
    elif critical_triggered:
        dual_approval_reason = "Critical Risk"
    elif blast_radius_triggered:
        dual_approval_reason = (
            f"Blast Radius ({device_count} devices, threshold "
            f"{settings.RISK_BLAST_RADIUS_DUAL_APPROVAL_THRESHOLD})"
        )

    cr = ChangeRequest(
        device_id=payload.device_id,
        additional_device_ids=additional_device_ids_json,
        submitted_by=current_user.id,
        priority=payload.priority,
        description=payload.description,
        business_justification=payload.business_justification,
        maintenance_window_start=payload.maintenance_window_start,
        maintenance_window_end=payload.maintenance_window_end,
        current_config=current_config,
        proposed_config=payload.proposed_config,
        config_diff=diff_text,
        risk_score=risk.risk_score,
        risk_findings="; ".join(risk.findings),
        risk_classification=risk.classification,
        requires_dual_approval=requires_dual_approval,
        dual_approval_reason=dual_approval_reason,
        canary_enabled=payload.canary_enabled,
        status=ChangeStatus.PENDING_APPROVAL if validation.passed else ChangeStatus.DRAFT,
    )
    db.add(cr)
    db.commit()
    db.refresh(cr)

    audit_service.record_event(
        db,
        actor=current_user.email,
        action="Submitted CR",
        result="Success" if validation.passed else "Validation Failed",
        device_hostname=device.hostname,
        change_request_id=cr.id,
        detail="; ".join(validation.errors) if validation.errors else None,
    )
    event_bus.publish_event("change_request_status_changed", status=cr.status.value, change_request_id=str(cr.id))

    return cr


@router.get("/{cr_id}", response_model=ChangeRequestRead)
def get_change_request(cr_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    cr = db.get(ChangeRequest, cr_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")
    return cr


@router.post("/{cr_id}/approve", response_model=ChangeRequestRead)
def approve_change_request(
    cr_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Approves a pending change request and enqueues the deployment
    pipeline (Snapshot -> Deploy -> Health Monitor -> Success/Rollback) as
    background Celery task(s) -- one per target device, run concurrently
    (SRS 6.6) -- rather than blocking this request until deployment
    finishes. Only Network Administrators may approve (RBAC, FR-1/FR-3).

    Returns immediately with status "approved"; poll GET /change-requests/{id}
    or GET /deployments?change_request_id={id} for progress.
    """
    if current_user.role not in APPROVER_ROLES:
        raise HTTPException(status_code=403, detail="Only Network Administrators may approve change requests")

    cr = db.get(ChangeRequest, cr_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")
    if cr.status != ChangeStatus.PENDING_APPROVAL:
        raise HTTPException(status_code=400, detail=f"Cannot approve a request in status '{cr.status.value}'")

    device = db.get(Device, cr.device_id)
    reason = cr.dual_approval_reason or "Critical Risk"

    # Dual approval (AI Configuration Analyzer, SRS 6.2 / FR-6, extended to
    # blast radius): a single approval is not enough. The first Network
    # Administrator's approval is recorded but does NOT move the request
    # out of PENDING_APPROVAL or enqueue deployment; a *different* Network
    # Administrator must approve again to finalize it. Triggered either by
    # Critical Risk classification or by additional_device_ids fanning the
    # change out past RISK_BLAST_RADIUS_DUAL_APPROVAL_THRESHOLD devices --
    # see cr.dual_approval_reason / app.api.change_requests.create_change_request.
    if cr.requires_dual_approval and cr.first_approved_by is None:
        cr.first_approved_by = current_user.id
        cr.first_approved_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(cr)

        audit_service.record_event(
            db, actor=current_user.email, action=f"First Approval ({reason})",
            result="Awaiting Second Approval",
            device_hostname=device.hostname if device else None, change_request_id=cr.id,
            detail=f"{reason}: a second, different Network Administrator must approve before deployment.",
        )
        event_bus.publish_event(
            "change_request_status_changed", status=cr.status.value, change_request_id=str(cr.id)
        )
        return cr

    if cr.requires_dual_approval and cr.first_approved_by == current_user.id:
        raise HTTPException(
            status_code=400,
            detail=f"{reason}: the second approval must come from a different Network Administrator.",
        )

    cr.status = ChangeStatus.APPROVED
    cr.approved_by = current_user.id
    db.commit()
    db.refresh(cr)

    action = f"Approved (2nd of 2, {reason})" if cr.requires_dual_approval else "Approved"
    audit_service.record_event(
        db, actor=current_user.email, action=action, result="Approved",
        device_hostname=device.hostname if device else None, change_request_id=cr.id,
    )
    event_bus.publish_event("change_request_status_changed", status=cr.status.value, change_request_id=str(cr.id))

    run_deployment_pipeline_task.delay(str(cr.id), current_user.email)
    return cr


@router.post("/{cr_id}/reject", response_model=ChangeRequestRead)
def reject_change_request(
    cr_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in APPROVER_ROLES:
        raise HTTPException(status_code=403, detail="Only Network Administrators may reject change requests")

    cr = db.get(ChangeRequest, cr_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")

    cr.status = ChangeStatus.REJECTED
    db.commit()
    db.refresh(cr)

    device = db.get(Device, cr.device_id)
    audit_service.record_event(
        db, actor=current_user.email, action="Rejected", result="Rejected",
        device_hostname=device.hostname if device else None, change_request_id=cr.id,
    )
    event_bus.publish_event("change_request_status_changed", status=cr.status.value, change_request_id=str(cr.id))
    return cr
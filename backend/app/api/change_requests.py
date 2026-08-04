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


def _resolve_current_config(db: Session, device: Device, current_user: User) -> tuple[str | None, str]:
    """Current config: prefer a fresh live read from the device itself (it's
    already been proven reachable -- see health_monitor/snmp -- so use that
    reachability instead of trusting a snapshot that may be stale or may
    not exist yet for a device that's never been deployed to). Falls back
    to the last snapshot on file if the live read fails for any reason
    (device briefly unreachable, no supported protocol configured, etc.).

    Returns (current_config, config_source) where config_source is "live",
    "snapshot", or "none" (no live read and no snapshot on file) --
    recorded on the CR so a reviewer can see how fresh the "current" side
    of the diff/risk analysis actually was, and so POST
    /change-requests/{id}/rescore has something concrete to retry.
    """
    pm = protocol_manager.ProtocolManager(db, device, operator=current_user.email)
    live_running = pm.get_running_config()
    if live_running.success:
        return live_running.output, "live"
    snapshot_config = _latest_config(db, device.id)
    if snapshot_config is not None:
        return snapshot_config, "snapshot"
    return None, "none"


def _score_change(
    db: Session, device: Device, proposed_config: str, current_user: User,
) -> dict:
    """Shared by create_change_request and rescore_change_request: resolves
    current_config (live, falling back to the last snapshot), runs
    validation + risk_engine.analyze, and returns every field derived from
    that -- so both endpoints stay in sync with exactly one implementation
    instead of the retry/rescore action drifting from what submission does.
    """
    current_config, config_source = _resolve_current_config(db, device, current_user)
    fleet_configs = _fleet_configs(db, device.id)
    diff_text = diff_engine.generate_diff(current_config, proposed_config)
    validation = validation_engine.validate_syntax(
        proposed_config,
        vendor=device.vendor.value if hasattr(device.vendor, "value") else device.vendor,
        current_config=current_config,
    )
    risk: RiskAnalysisResult = risk_engine.analyze(proposed_config, current_config, fleet_configs)
    return {
        "current_config": current_config,
        "config_source": config_source,
        "config_diff": diff_text,
        "validation": validation,
        "risk": risk,
    }


def _dual_approval(risk: RiskAnalysisResult, device_count: int) -> tuple[bool, str | None]:
    """Blast-radius dual approval (SRS 6.2 / FR-6 extension): a change
    fanned out to enough devices requires two distinct Network
    Administrator approvals regardless of its individual risk score -- a
    low-risk change pushed to 50 devices is still high blast-radius.
    Shared by create and rescore so a rescore can't silently drop a dual-
    approval requirement the original submission had (or vice versa)
    through separately-maintained logic.
    """
    critical = risk_engine.is_critical(risk)
    blast_radius_triggered = device_count > settings.RISK_BLAST_RADIUS_DUAL_APPROVAL_THRESHOLD
    critical_triggered = critical and settings.RISK_CRITICAL_DUAL_APPROVAL_ENABLED
    requires_dual_approval = critical_triggered or blast_radius_triggered
    reason = None
    if critical_triggered and blast_radius_triggered:
        reason = "Critical Risk + Blast Radius"
    elif critical_triggered:
        reason = "Critical Risk"
    elif blast_radius_triggered:
        reason = (
            f"Blast Radius ({device_count} devices, threshold "
            f"{settings.RISK_BLAST_RADIUS_DUAL_APPROVAL_THRESHOLD})"
        )
    return requires_dual_approval, reason


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

    result = _score_change(db, device, payload.proposed_config, current_user)
    current_config, config_source = result["current_config"], result["config_source"]
    diff_text, validation, risk = result["config_diff"], result["validation"], result["risk"]

    device_count = 1 + len(payload.additional_device_ids or [])
    requires_dual_approval, dual_approval_reason = _dual_approval(risk, device_count)

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
        config_source=config_source,
        proposed_config=payload.proposed_config,
        config_diff=diff_text,
        validation_passed="true" if validation.passed else "false",
        validation_errors=json.dumps(validation.errors) if validation.errors else None,
        validation_warnings=json.dumps(validation.warnings) if validation.warnings else None,
        risk_score=risk.risk_score,
        risk_findings="; ".join(risk.findings),
        risk_classification=risk.classification,
        risk_engine_backend=settings.RISK_ENGINE_BACKEND,
        risk_llm_applied=risk.llm_applied,
        risk_llm_error=risk.llm_error,
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


# Only DRAFT/PENDING_APPROVAL CRs can be rescored -- once a CR has been
# approved (or moved beyond), the analysis that was approved must stay
# exactly what gets deployed; rescoring after that point would silently
# change what's about to ship without a fresh approval.
RESCORE_ALLOWED_STATUSES = (ChangeStatus.DRAFT, ChangeStatus.PENDING_APPROVAL)


@router.post("/{cr_id}/rescore", response_model=ChangeRequestRead)
def rescore_change_request(
    cr_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retry/re-score an existing change request in place, instead of the
    submitter having to discard it and resubmit from scratch.

    Two situations this exists for, both visible on the CR detail page:
      - config_source came back "snapshot"/"none" -- the live device read
        failed at submission time (device briefly unreachable, etc.) and
        the analysis ran against a stale/absent snapshot instead.
      - risk_llm_applied is False despite risk_engine_backend == "llm" --
        the model call didn't actually run (see risk_llm_error for why:
        no credential, Ollama/Anthropic unreachable, bad response, ...).

    Re-runs the exact same live-read-with-snapshot-fallback + validation +
    risk_engine.analyze used at submission (see _score_change), then
    overwrites this CR's current_config/config_diff/validation_*/risk_*/
    dual-approval fields with the fresh result. Only allowed while the CR
    hasn't been acted on yet (DRAFT or PENDING_APPROVAL).
    """
    cr = db.get(ChangeRequest, cr_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")
    if cr.status not in RESCORE_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot rescore a change request in '{cr.status.value}' status -- "
                "only draft or pending-approval change requests can be rescored."
            ),
        )

    device = db.get(Device, cr.device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    result = _score_change(db, device, cr.proposed_config, current_user)
    validation, risk = result["validation"], result["risk"]

    import json
    device_count = 1 + (len(json.loads(cr.additional_device_ids)) if cr.additional_device_ids else 0)
    requires_dual_approval, dual_approval_reason = _dual_approval(risk, device_count)

    cr.current_config = result["current_config"]
    cr.config_source = result["config_source"]
    cr.config_diff = result["config_diff"]
    cr.validation_passed = "true" if validation.passed else "false"
    cr.validation_errors = json.dumps(validation.errors) if validation.errors else None
    cr.validation_warnings = json.dumps(validation.warnings) if validation.warnings else None
    cr.risk_score = risk.risk_score
    cr.risk_findings = "; ".join(risk.findings)
    cr.risk_classification = risk.classification
    cr.risk_engine_backend = settings.RISK_ENGINE_BACKEND
    cr.risk_llm_applied = risk.llm_applied
    cr.risk_llm_error = risk.llm_error
    cr.requires_dual_approval = requires_dual_approval
    cr.dual_approval_reason = dual_approval_reason
    # A rescore that now passes validation moves a DRAFT into the approval
    # queue; one that now fails pulls a PENDING_APPROVAL CR back to draft
    # rather than leaving it approvable with a failing validation result.
    cr.status = ChangeStatus.PENDING_APPROVAL if validation.passed else ChangeStatus.DRAFT

    db.commit()
    db.refresh(cr)

    audit_service.record_event(
        db, actor=current_user.email, action="Rescored CR",
        result="Success" if validation.passed else "Validation Failed",
        device_hostname=device.hostname, change_request_id=cr.id,
        detail=(
            f"config_source={result['config_source']} risk_score={risk.risk_score} "
            f"llm_applied={risk.llm_applied}" + (f" llm_error={risk.llm_error}" if risk.llm_error else "")
        ),
    )
    event_bus.publish_event("change_request_status_changed", status=cr.status.value, change_request_id=str(cr.id))

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

    # Maintenance window enforcement (SRS change-freeze / compliance
    # requirement): maintenance_window_start/end have been captured on the
    # change request since submission but, until now, were never actually
    # checked -- approving a change enqueued its deployment immediately
    # regardless of the declared window, so nothing stopped a change from
    # firing outside a scheduled freeze/maintenance period.
    #
    # A change with no window declared deploys immediately, same as
    # before (not every shop declares one). One with a window:
    #   - already within it right now -> deploy immediately, as before.
    #   - starts in the future -> APPROVED now, but the deployment task is
    #     scheduled via Celery's `eta` for the window's actual start
    #     instead of firing right away -- pre-approving ahead of a
    #     maintenance window is a normal workflow, deploying outside it
    #     is not.
    #   - has already ended -> approval is refused outright (422); the
    #     change must be resubmitted with a current window rather than
    #     silently deploying late outside its declared freeze period.
    now = datetime.now(timezone.utc)
    window_start = cr.maintenance_window_start
    window_end = cr.maintenance_window_end

    if window_end is not None and now > window_end:
        cr.status = ChangeStatus.APPROVED  # leave the approval on record...
        db.commit()
        audit_service.record_event(
            db, actor=current_user.email, action="Deployment Blocked — Maintenance Window Expired",
            result="Blocked", device_hostname=device.hostname if device else None, change_request_id=cr.id,
            detail=f"Window was {window_start} - {window_end}; approval happened at {now}.",
        )
        raise HTTPException(
            status_code=422,
            detail=(
                f"This change's maintenance window ({window_start} - {window_end}) has already passed. "
                "It has been recorded as approved, but deployment was not scheduled -- resubmit with a "
                "current maintenance window to deploy it."
            ),
        )

    if window_start is not None and now < window_start:
        run_deployment_pipeline_task.apply_async(
            args=[str(cr.id), current_user.email], eta=window_start
        )
        audit_service.record_event(
            db, actor=current_user.email, action="Deployment Scheduled For Maintenance Window", result="Scheduled",
            device_hostname=device.hostname if device else None, change_request_id=cr.id,
            detail=f"Scheduled for {window_start} (window ends {window_end}).",
        )
        return cr

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
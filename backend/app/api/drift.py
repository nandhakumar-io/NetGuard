import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.config_drift import ConfigDrift, DriftStatus
from app.models.device import Device
from app.models.user import User, UserRole
from app.schemas.drift import (
    DriftDetail,
    DriftFleetSummary,
    DriftRead,
    DriftScanRequest,
    DriftScanResponse,
    DriftStatusUpdate,
    RollbackRecommendationResponse,
)
from app.services import audit_service, drift_service

router = APIRouter(tags=["drift"])

# Reviewing/dismissing/approving a drift changes its record of what's
# "acceptable" on the device, same authority level as approving a change.
DRIFT_REVIEW_ROLES = require_roles(UserRole.NETWORK_ADMIN)


@router.get("/drift/summary", response_model=DriftFleetSummary)
def get_fleet_drift_summary(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Powers the Drift Dashboard Widget (fleet-wide drift posture)."""
    return drift_service.fleet_summary(db)


@router.get("/drift", response_model=list[DriftRead])
def list_all_drifts(
    status: DriftStatus | None = None,
    severity: str | None = None,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Fleet-wide drift feed for the Drift page, newest first."""
    from app.models.config_drift import DriftSeverity

    sev = DriftSeverity(severity) if severity else None
    return drift_service.list_drifts(db, status=status, severity=sev)


@router.get("/drift/{drift_id}", response_model=DriftDetail)
def get_drift(drift_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    drift = db.get(ConfigDrift, drift_id)
    if not drift:
        raise HTTPException(status_code=404, detail="Drift record not found")
    return drift


@router.get("/drift/{drift_id}/rollback-recommendation", response_model=RollbackRecommendationResponse)
def get_rollback_recommendation(drift_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    drift = db.get(ConfigDrift, drift_id)
    if not drift:
        raise HTTPException(status_code=404, detail="Drift record not found")
    return drift_service.rollback_recommendation(drift)


@router.patch("/drift/{drift_id}", response_model=DriftRead)
def update_drift_status(
    drift_id: uuid.UUID,
    payload: DriftStatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(DRIFT_REVIEW_ROLES),
):
    """Mark a drift as approved (accepted as the new normal), dismissed
    (false positive / expected), or manually reconciled -- does not touch
    the device itself. To actually fix the device, use
    POST /devices/{id}/rollback with a snapshot, same as any other
    rollback; that flow can set this record's status to ROLLED_BACK
    separately once it succeeds.
    """
    drift = db.get(ConfigDrift, drift_id)
    if not drift:
        raise HTTPException(status_code=404, detail="Drift record not found")

    device = db.get(Device, drift.device_id)
    drift.status = payload.status
    db.commit()
    db.refresh(drift)

    audit_service.record_event(
        db,
        actor=current_user.email,
        action="Drift Reviewed",
        result=payload.status.value,
        device_hostname=device.hostname if device else None,
        detail=f"drift_id={drift.id}",
    )
    return drift


@router.get("/devices/{device_id}/drift", response_model=list[DriftRead])
def list_device_drift_history(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return drift_service.list_drifts(db, device_id=device_id)


@router.post("/devices/{device_id}/drift/scan", response_model=DriftScanResponse)
def scan_device_drift(
    device_id: uuid.UUID,
    payload: DriftScanRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """On-demand drift scan (SRS: nightly automated + on-demand). Reads the
    device's live running config, compares it against the requested
    baseline, and persists a new ConfigDrift record.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    try:
        result = drift_service.detect_drift(db, device, baseline=payload.baseline, triggered_by=current_user.email)
    except drift_service.NoBaselineError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return DriftScanResponse(
        drift=result.drift,
        baseline_label=result.baseline_label,
        findings=result.findings,
        rollback_recommendation=drift_service.rollback_recommendation(result.drift),
    )
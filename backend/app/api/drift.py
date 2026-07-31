import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.config_drift import ConfigDrift
from app.models.device import Device
from app.models.user import User, UserRole
from app.schemas.config_drift import ConfigDriftRead
from app.services import credential_service, drift_engine, event_bus, notification_service

router = APIRouter(prefix="/devices/{device_id}/drift", tags=["config-drift"])

# On-demand checks and resolving a drift finding both touch device
# credentials / audit posture -- restrict to admins, same as inventory
# management (devices.py).
DRIFT_MANAGER_ROLES = require_roles(UserRole.NETWORK_ADMIN)


def _get_device_or_404(db: Session, device_id: uuid.UUID) -> Device:
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


@router.get("", response_model=list[ConfigDriftRead])
def list_drift_history(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    _get_device_or_404(db, device_id)
    return (
        db.query(ConfigDrift)
        .filter(ConfigDrift.device_id == device_id)
        .order_by(ConfigDrift.checked_at.desc())
        .all()
    )


@router.post("/check", response_model=ConfigDriftRead, status_code=201)
def trigger_drift_check(
    device_id: uuid.UUID, db: Session = Depends(get_db), current_user: User = Depends(DRIFT_MANAGER_ROLES),
):
    """Runs a drift check right now (rather than waiting for the next
    scheduled sweep). Synchronous -- a single device's SSH round-trip is
    fast enough not to warrant a Celery task for the on-demand case.
    """
    device = _get_device_or_404(db, device_id)

    try:
        password = credential_service.get_ssh_password(device)
    except credential_service.CredentialNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    record = drift_engine.check_device_drift(
        db, device, username=device.ssh_username or "admin", password=password, triggered_by="manual",
    )

    if record.drifted == "true":
        notification_service.notify(
            "Config Drift Detected",
            f"{device.hostname}: {record.detail} (severity={record.severity.value}, "
            f"checked manually by {current_user.email})",
            severity="warning" if record.severity.value in ("low", "medium") else "critical",
        )
        event_bus.publish_event("config_drift_detected", device=device.hostname, severity=record.severity.value)

    return record


@router.post("/{drift_id}/resolve", response_model=ConfigDriftRead)
def resolve_drift(
    device_id: uuid.UUID,
    drift_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(DRIFT_MANAGER_ROLES),
):
    """Marks a drift finding as reviewed/accepted (e.g. an admin confirmed
    the out-of-band change was legitimate and doesn't want it flagged
    again). Does not touch the device or its snapshots -- purely a
    record-keeping acknowledgement.
    """
    from datetime import datetime, timezone

    record = db.get(ConfigDrift, drift_id)
    if not record or record.device_id != device_id:
        raise HTTPException(status_code=404, detail="Drift record not found for this device")

    record.resolved = "true"
    record.resolved_at = datetime.now(timezone.utc)
    record.resolved_by = current_user.email
    db.commit()
    db.refresh(record)
    event_bus.publish_event("config_drift_resolved", device_id=str(device_id))
    return record

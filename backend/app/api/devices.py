import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.device import Device
from app.models.protocol_operation import ProtocolOperation
from app.models.snapshot import ConfigSnapshot
from app.models.user import User, UserRole
from app.schemas.device import DeviceCreate, DeviceRead, DeviceUpdate
from app.schemas.rollback import RollbackRequest, RollbackResponse, SnapshotSummary
from app.services import rollback_service, audit_service, metrics_service
from app.tasks import run_deployment_pipeline_task

router = APIRouter(prefix="/devices", tags=["devices"])

# Only Network Administrators manage inventory (FR-2 + RBAC); everyone authenticated can read it.
INVENTORY_MANAGER_ROLES = require_roles(UserRole.NETWORK_ADMIN)


def _poll_snmp_best_effort(db: Session, device: Device) -> None:
    """Fire an immediate SNMP poll synchronously (instead of queuing a
    Celery task) so the dashboard/health tab has telemetry right away even
    when no Celery worker/Redis is running -- see also the in-process
    polling loop in app.main for the recurring sweep. Best-effort: an
    unreachable device or missing credential must not block the device
    create/update response.
    """
    try:
        metrics_service.poll_device(db, device)
    except metrics_service.SnmpNotConfiguredError:
        pass
    except metrics_service.credential_service.CredentialNotFoundError:
        pass
    except Exception:  # noqa: BLE001 - best-effort only
        pass

# Rollback carries the same authority as approving a change (both bypass
# the normal validation/approval queue), so it's gated the same way.
ROLLBACK_ROLES = require_roles(UserRole.NETWORK_ADMIN)


@router.get("", response_model=list[DeviceRead])
def list_devices(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(Device).all()


@router.post("", response_model=DeviceRead, status_code=201)
def create_device(payload: DeviceCreate, db: Session = Depends(get_db), _=Depends(INVENTORY_MANAGER_ROLES)):
    if db.query(Device).filter(Device.hostname == payload.hostname).first():
        raise HTTPException(status_code=400, detail="Device with this hostname already exists")
    device = Device(**payload.model_dump())
    db.add(device)
    db.commit()
    db.refresh(device)

    # Don't make the operator wait for the next SNMP_POLL_INTERVAL_SECONDS
    # sweep -- if the device was added with SNMP already configured, poll
    # it right away so the dashboard / device detail page has telemetry as
    # soon as possible.
    if device.supports_snmp:
        _poll_snmp_best_effort(db, device)

    return device


@router.get("/{device_id}", response_model=DeviceRead)
def get_device(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


@router.patch("/{device_id}", response_model=DeviceRead)
def update_device(
    device_id: uuid.UUID, payload: DeviceUpdate, db: Session = Depends(get_db), _=Depends(INVENTORY_MANAGER_ROLES)
):
    """Partial update -- e.g. enabling SNMP monitoring on a device that was
    added before its community string / SNMPv3 credentials were set up.
    Only fields present in the request body are changed.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    updates = payload.model_dump(exclude_unset=True)
    if "hostname" in updates and updates["hostname"] != device.hostname:
        if db.query(Device).filter(Device.hostname == updates["hostname"]).first():
            raise HTTPException(status_code=400, detail="Device with this hostname already exists")

    was_snmp_enabled = device.supports_snmp

    for field, value in updates.items():
        setattr(device, field, value)

    db.commit()
    db.refresh(device)

    if device.supports_snmp and not was_snmp_enabled:
        _poll_snmp_best_effort(db, device)

    return device


@router.delete("/{device_id}", status_code=204)
def delete_device(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(INVENTORY_MANAGER_ROLES)):
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    db.delete(device)
    db.commit()


@router.get("/{device_id}/snapshots", response_model=list[SnapshotSummary])
def list_device_snapshots(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Config version history for a device (SRS 10: git-style config
    version control), newest first. Pick a `version`'s `id` to pass to
    POST /devices/{device_id}/rollback.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return rollback_service.list_snapshots(db, device_id)


@router.post("/{device_id}/rollback", response_model=RollbackResponse, status_code=202)
def rollback_device(
    device_id: uuid.UUID,
    payload: RollbackRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(ROLLBACK_ROLES),
):
    """Manually roll a device back to a prior configuration snapshot.

    Builds and auto-approves a change request that redeploys the chosen
    snapshot, then queues it on the same deployment pipeline used for
    ordinary approved changes (Snapshot -> Deploy -> Health Monitor ->
    Success / Automatic Rollback) -- so the restore itself is snapshotted
    first and its health is verified just like any other deployment.

    Returns immediately (202) with the change_request_id; poll
    GET /change-requests/{id} or GET /deployments?change_request_id={id}
    for progress, same as approving a normal change request.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    snapshot = db.get(ConfigSnapshot, payload.snapshot_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    try:
        cr = rollback_service.initiate_rollback(db, device, snapshot, current_user, reason=payload.reason)
    except rollback_service.RollbackError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    run_deployment_pipeline_task.delay(str(cr.id), current_user.email)

    return RollbackResponse(
        change_request_id=cr.id,
        status=cr.status.value,
        message=f"Rollback queued for {device.hostname}. Track progress via the change request or deployments feed.",
    )


@router.post("/{device_id}/clear-unstable-flag", response_model=DeviceRead)
def clear_unstable_flag(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(INVENTORY_MANAGER_ROLES),
):
    """Manual review sign-off for the deployment pipeline circuit breaker:
    clears `flagged_unstable` so automated deploys against this device are
    allowed again. Only Network Administrators may clear it (same RBAC as
    inventory management / rollback) -- this is a deliberate "I've looked
    at what's wrong with this device" action, never automatic.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if not device.flagged_unstable:
        raise HTTPException(status_code=400, detail="Device is not currently flagged unstable")

    device.flagged_unstable = False
    device.unstable_since = None
    db.commit()
    db.refresh(device)

    audit_service.record_event(
        db, actor=current_user.email, action="Unstable Flag Cleared", result="Success",
        device_hostname=device.hostname,
        detail="Manual review completed; automated deploys re-enabled.",
    )
    return device


@router.get("/{device_id}/protocol-operations")
def list_device_protocol_operations(
    device_id: uuid.UUID,
    limit: int = 50,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Recent NETCONF/RESTCONF/SNMP operations recorded against this device
    (config reads/pushes, health checks, SNMP polls) -- backs the Protocol
    Operations tab on the device detail page. Complements the coarser
    AuditLog with the raw request/response payloads captured by
    ProtocolManager for every operation it performs.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    ops = (
        db.query(ProtocolOperation)
        .filter(ProtocolOperation.device_id == device_id)
        .order_by(ProtocolOperation.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": str(op.id),
            "protocol": op.protocol.value if hasattr(op.protocol, "value") else str(op.protocol),
            "operation": op.operation,
            "operator": op.operator,
            "success": op.success,
            "error_message": op.error_message,
            "http_status": op.http_status,
            "execution_time_ms": op.execution_time_ms,
            "created_at": op.created_at,
        }
        for op in ops
    ]
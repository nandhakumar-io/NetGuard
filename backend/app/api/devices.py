import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.device import Device
from app.models.snapshot import ConfigSnapshot
from app.models.user import User, UserRole
from app.schemas.device import DeviceCreate, DeviceRead, DeviceUpdate
from app.schemas.rollback import RollbackRequest, RollbackResponse, SnapshotSummary
from app.services import rollback_service
from app.tasks import run_deployment_pipeline_task

router = APIRouter(prefix="/devices", tags=["devices"])

# Only Network Administrators manage inventory (FR-2 + RBAC); everyone authenticated can read it.
INVENTORY_MANAGER_ROLES = require_roles(UserRole.NETWORK_ADMIN)

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

    for field, value in updates.items():
        setattr(device, field, value)

    db.commit()
    db.refresh(device)
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
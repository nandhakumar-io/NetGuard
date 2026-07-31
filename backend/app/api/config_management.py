import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.device import Device
from app.models.snapshot import ConfigSnapshot
from app.models.user import User, UserRole
from app.schemas.config_management import (
    BackupConfigRequest,
    BackupConfigResponse,
    BackupHistoryEntry,
    CompareConfigRequest,
    CompareConfigResponse,
    RestoreConfigRequest,
    RestoreConfigResponse,
    RunningConfigResponse,
    StartupConfigResponse,
)
from app.services import audit_service, diff_engine, snapshot_service
from app.services.protocol_manager import ProtocolManager
from app.services.rollback_service import list_snapshots

router = APIRouter(prefix="/devices/{device_id}/config", tags=["configuration-management"])

# Backups and restores are config-changing/authority-bearing operations
# (a restore is a live config push), so they're gated the same way
# rollback is in app.api.devices: Network Administrators only. Viewing
# (running/startup/backup history/compare) is available to any
# authenticated user, matching read access elsewhere in the app.
CONFIG_WRITE_ROLES = require_roles(UserRole.NETWORK_ADMIN)


def _get_device(db: Session, device_id: uuid.UUID) -> Device:
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


def _latest_snapshot(db: Session, device_id: uuid.UUID) -> ConfigSnapshot | None:
    return (
        db.query(ConfigSnapshot)
        .filter(ConfigSnapshot.device_id == device_id)
        .order_by(ConfigSnapshot.created_at.desc())
        .first()
    )


def _get_snapshot_for_device(db: Session, device_id: uuid.UUID, snapshot_id: uuid.UUID) -> ConfigSnapshot:
    snapshot = db.get(ConfigSnapshot, snapshot_id)
    if not snapshot or snapshot.device_id != device_id:
        raise HTTPException(status_code=404, detail="Snapshot not found for this device")
    return snapshot


# ---------------------------------------------------------------------------
# View Running Config
# ---------------------------------------------------------------------------
@router.get("/running", response_model=RunningConfigResponse)
def view_running_config(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    device = _get_device(db, device_id)
    pm = ProtocolManager(db, device)
    result = pm.get_running_config()
    if not result.success:
        raise HTTPException(status_code=502, detail=result.error or "Failed to read running configuration")
    return RunningConfigResponse(
        device_id=device.id,
        hostname=device.hostname,
        protocol=result.protocol.value if hasattr(result.protocol, "value") else str(result.protocol),
        config=result.output,
        retrieved_at=datetime.datetime.utcnow(),
    )


# ---------------------------------------------------------------------------
# View Startup Config
# ---------------------------------------------------------------------------
@router.get("/startup", response_model=StartupConfigResponse)
def view_startup_config(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    device = _get_device(db, device_id)
    snapshot = _latest_snapshot(db, device_id)
    if snapshot is None or not snapshot.startup_config_encrypted:
        return StartupConfigResponse(
            device_id=device.id,
            hostname=device.hostname,
            config=None,
            source="unavailable",
            snapshot_id=None,
            retrieved_at=datetime.datetime.utcnow(),
        )
    return StartupConfigResponse(
        device_id=device.id,
        hostname=device.hostname,
        config=snapshot_service.decrypt_config(snapshot.startup_config_encrypted),
        source="snapshot",
        snapshot_id=snapshot.id,
        retrieved_at=datetime.datetime.utcnow(),
    )


# ---------------------------------------------------------------------------
# Backup Config  (+ Backup History)
# ---------------------------------------------------------------------------
def _to_history_entry(snapshot: ConfigSnapshot) -> BackupHistoryEntry:
    return BackupHistoryEntry(
        id=snapshot.id,
        device_id=snapshot.device_id,
        change_request_id=snapshot.change_request_id,
        version=snapshot.version,
        checksum=snapshot.checksum,
        has_startup_config=bool(snapshot.startup_config_encrypted),
        created_at=snapshot.created_at,
    )


@router.post("/backup", response_model=BackupConfigResponse, status_code=201)
def backup_config(
    device_id: uuid.UUID,
    payload: BackupConfigRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(CONFIG_WRITE_ROLES),
):
    """On-demand configuration backup (FR: Backup Config).

    Reads the device's live running config via the Protocol Manager and
    persists it as an immutable ConfigSnapshot -- the same storage used by
    the automatic pre-deployment snapshot and the rollback history, so a
    manual backup shows up in Backup History / snapshot history either way.
    """
    device = _get_device(db, device_id)
    pm = ProtocolManager(db, device, operator=current_user.email)
    result = pm.backup_config()
    if not result.success:
        raise HTTPException(status_code=502, detail=result.error or "Failed to read device configuration for backup")

    version = str(int(datetime.datetime.utcnow().timestamp()))
    payload_dict = snapshot_service.build_snapshot_payload(result.output, None, version)
    snapshot = ConfigSnapshot(device_id=device.id, seq=snapshot_service.next_seq(db), **payload_dict)
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)

    label = (payload.label if payload else None) or "manual backup"
    audit_service.record_event(
        db,
        actor=current_user.email,
        action="Configuration Backup",
        result="Success",
        device_hostname=device.hostname,
        detail=f"snapshot={snapshot.id} version={snapshot.version} label={label}",
    )

    return BackupConfigResponse(
        snapshot=_to_history_entry(snapshot),
        protocol=result.protocol.value if hasattr(result.protocol, "value") else str(result.protocol),
        message=f"Backed up {device.hostname} configuration (v{snapshot.version}).",
    )


@router.get("/backups", response_model=list[BackupHistoryEntry])
def backup_history(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Full backup / configuration version history for a device, newest first."""
    device = _get_device(db, device_id)
    snapshots = list_snapshots(db, device.id)
    return [_to_history_entry(s) for s in snapshots]


# ---------------------------------------------------------------------------
# Download Config
# ---------------------------------------------------------------------------
@router.get("/backups/{snapshot_id}/download")
def download_backup(
    device_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    device = _get_device(db, device_id)
    snapshot = _get_snapshot_for_device(db, device_id, snapshot_id)
    config_text = snapshot_service.decrypt_config(snapshot.running_config_encrypted)
    filename = f"{device.hostname}_v{snapshot.version}_{snapshot.checksum[:8]}.cfg"
    return Response(
        content=config_text,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Restore Config
# ---------------------------------------------------------------------------
@router.post("/restore", response_model=RestoreConfigResponse)
def restore_config(
    device_id: uuid.UUID,
    payload: RestoreConfigRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(CONFIG_WRITE_ROLES),
):
    """Restore a device directly to a prior backup (FR: Restore Config).

    This is a direct, immediate config push via the Protocol Manager --
    for a governed restore that goes through the full
    snapshot -> deploy -> health-monitor -> auto-rollback pipeline as a
    tracked Change Request instead, use POST /devices/{id}/rollback.
    A pre-restore snapshot of the device's current state is still taken
    first so the restore itself is never undoable, and a post-restore
    snapshot is captured on success to keep the backup history accurate.
    """
    device = _get_device(db, device_id)
    snapshot = _get_snapshot_for_device(db, device_id, payload.snapshot_id)
    restored_config = snapshot_service.decrypt_config(snapshot.running_config_encrypted)

    pm = ProtocolManager(db, device, operator=current_user.email)

    # Pre-restore safety snapshot of whatever is live right now.
    pre_restore = pm.backup_config()
    if pre_restore.success:
        pre_version = str(int(datetime.datetime.utcnow().timestamp()))
        pre_payload = snapshot_service.build_snapshot_payload(pre_restore.output, None, pre_version)
        db.add(ConfigSnapshot(device_id=device.id, seq=snapshot_service.next_seq(db), **pre_payload))
        db.commit()

    result = pm.restore_config(restored_config)

    post_restore_id = None
    if result.success:
        post = pm.backup_config()
        if post.success:
            post_version = str(int(datetime.datetime.utcnow().timestamp()))
            post_payload = snapshot_service.build_snapshot_payload(post.output, None, post_version)
            post_snapshot = ConfigSnapshot(device_id=device.id, seq=snapshot_service.next_seq(db), **post_payload)
            db.add(post_snapshot)
            db.commit()
            db.refresh(post_snapshot)
            post_restore_id = post_snapshot.id

    audit_service.record_event(
        db,
        actor=current_user.email,
        action="Configuration Restore",
        result="Success" if result.success else "Failed",
        device_hostname=device.hostname,
        detail=(
            f"restored_from={snapshot.id} version={snapshot.version}"
            + (f" reason={payload.reason}" if payload.reason else "")
            + ("" if result.success else f" error={result.error}")
        ),
    )

    if not result.success:
        raise HTTPException(status_code=502, detail=result.error or "Failed to restore configuration to device")

    return RestoreConfigResponse(
        device_id=device.id,
        hostname=device.hostname,
        restored_from_snapshot_id=snapshot.id,
        post_restore_snapshot_id=post_restore_id,
        protocol=result.protocol.value if hasattr(result.protocol, "value") else str(result.protocol),
        success=True,
        message=f"Restored {device.hostname} to configuration v{snapshot.version}.",
    )


# ---------------------------------------------------------------------------
# Compare Configurations
# ---------------------------------------------------------------------------
@router.post("/compare", response_model=CompareConfigResponse)
def compare_config(
    device_id: uuid.UUID,
    payload: CompareConfigRequest,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    device = _get_device(db, device_id)

    def _resolve(snapshot_id: uuid.UUID | None) -> tuple[str, str]:
        """Returns (label, config_text). None => live running config."""
        if snapshot_id is None:
            pm = ProtocolManager(db, device)
            result = pm.get_running_config()
            if not result.success:
                raise HTTPException(status_code=502, detail=result.error or "Failed to read live running config")
            return "live running config", result.output
        snap = _get_snapshot_for_device(db, device_id, snapshot_id)
        return f"backup v{snap.version}", snapshot_service.decrypt_config(snap.running_config_encrypted)

    if payload.base_snapshot_id is None and payload.target_snapshot_id is None:
        latest = _latest_snapshot(db, device_id)
        if latest is None:
            raise HTTPException(status_code=404, detail="No backups exist yet for this device to compare against.")
        base_label, base_config = f"backup v{latest.version}", snapshot_service.decrypt_config(
            latest.running_config_encrypted
        )
        target_label, target_config = _resolve(None)
    else:
        base_label, base_config = _resolve(payload.base_snapshot_id)
        target_label, target_config = _resolve(payload.target_snapshot_id)

    diff = diff_engine.generate_diff(base_config, target_config)

    return CompareConfigResponse(
        device_id=device.id,
        base_label=base_label,
        target_label=target_label,
        identical=(base_config == target_config),
        diff=diff,
    )
"""Database Backups: on-demand pg_dump of the NetGuard application
database, with history. See app.services.backup_service for the module
docstring on scope (NetGuard's own DB, not device configs -- those are
app.services.snapshot_service's job).

Restricted to NETWORK_ADMIN throughout -- a backup file is effectively a
full export of every user's data (credentials table included, see
app.models.device's encrypted-at-rest columns), so it gets the same
access bar as credential rotation and device delete.
"""
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_roles
from app.models.backup_destination import BackupDestination
from app.models.backup_job import BackupJob
from app.models.device import Device
from app.models.user import User, UserRole
from app.schemas.backup_destination import (
    BackupDestinationCreate,
    BackupDestinationRead,
    BackupDestinationUpdate,
)
from app.services import audit_service, backup_destination_service, backup_service

router = APIRouter(prefix="/backups", tags=["backups"])

_admin_only = require_roles(UserRole.NETWORK_ADMIN)


class FleetConfigBackupRequest(BaseModel):
    # None (the default) means "every managed device" -- matches the
    # fleet-wide "Back Up All Devices Now" button on the Backups page.
    # A specific list lets the same endpoint back a "back up these
    # selected devices" flow later without a second route.
    device_ids: list[uuid.UUID] | None = None


def _serialize(job: BackupJob) -> dict:
    offsite_results = None
    if job.offsite_results:
        try:
            offsite_results = json.loads(job.offsite_results)
        except (ValueError, TypeError):
            offsite_results = None
    return {
        "id": str(job.id),
        "status": job.status,
        "file_name": job.file_path.rsplit("/", 1)[-1] if job.file_path else None,
        "size_bytes": job.size_bytes,
        "error_message": job.error_message,
        "triggered_by": job.triggered_by,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "duration_seconds": job.duration_seconds,
        "offsite_results": offsite_results,
    }


def _serialize_destination(dest: BackupDestination) -> BackupDestinationRead:
    config = backup_destination_service.decrypt_config(dest.config_encrypted)
    return BackupDestinationRead(
        id=str(dest.id),
        name=dest.name,
        type=dest.type,
        enabled=dest.enabled,
        config=backup_destination_service.masked_config(dest.type, config),
        created_by=dest.created_by,
        created_at=dest.created_at,
        last_run_at=dest.last_run_at,
        last_run_status=dest.last_run_status,
        last_error=dest.last_error,
    )


@router.get("")
def list_backups(db: Session = Depends(get_db), _: User = Depends(_admin_only)):
    """Archive history plus the summary stat-card figures the Backups page
    header shows (total/completed/failed counts, total bytes stored
    locally across completed rows)."""
    backup_service._reconcile_stuck_jobs(db)
    jobs = db.query(BackupJob).order_by(BackupJob.started_at.desc()).all()
    completed = [j for j in jobs if j.status == "completed"]
    failed = [j for j in jobs if j.status == "failed"]
    return {
        "backups": [_serialize(j) for j in jobs],
        "total": len(jobs),
        "completed": len(completed),
        "failed": len(failed),
        "stored_bytes": sum(j.size_bytes or 0 for j in completed),
    }


@router.post("/database", status_code=201)
def trigger_database_backup(db: Session = Depends(get_db), current_user: User = Depends(_admin_only)):
    """Runs a pg_dump of the NetGuard database right now and returns the
    resulting row -- status is "completed" or "failed" on the same
    response (see backup_service.run_database_backup's docstring for why
    a dump failure doesn't raise an HTTP error here)."""
    try:
        job = backup_service.run_database_backup(db, triggered_by_email=current_user.email)
    except backup_service.BackupError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return _serialize(job)


# ---------------------------------------------------------------------------
# Device configuration backups
#
# Complements the /devices/{device_id}/config/backup single-device endpoint
# (app.api.config_management, unchanged for the device's own Config tab)
# with the fleet-wide view the Backups page needs: which devices have been
# backed up, when, and a one-click "back up everything now" sweep -- see
# app.services.backup_service.device_backup_summary / run_fleet_config_backup.
# ---------------------------------------------------------------------------
@router.get("/devices")
def list_device_backups(db: Session = Depends(get_db), _: User = Depends(_admin_only)):
    """Per-device config backup coverage for the Backups page: last backup
    time/version, how many are on file, and days since the last one --
    sorted so devices needing attention (never backed up, or most overdue)
    come first."""
    rows = backup_service.device_backup_summary(db)
    never_backed_up = sum(1 for r in rows if r["last_backup_at"] is None)
    return {
        "devices": [
            {
                "device_id": str(r["device_id"]),
                "hostname": r["hostname"],
                "ip_address": r["ip_address"],
                "vendor": r["vendor"],
                "backup_count": r["backup_count"],
                "last_backup_at": r["last_backup_at"].isoformat() if r["last_backup_at"] else None,
                "last_backup_version": r["last_backup_version"],
                "last_backup_snapshot_id": str(r["last_backup_snapshot_id"]) if r["last_backup_snapshot_id"] else None,
                "days_since_backup": r["days_since_backup"],
            }
            for r in rows
        ],
        "total_devices": len(rows),
        "never_backed_up": never_backed_up,
    }


@router.post("/devices/{device_id}", status_code=201)
def trigger_device_backup(device_id: uuid.UUID, db: Session = Depends(get_db), current_user: User = Depends(_admin_only)):
    """Backs up a single device's config right now -- same underlying call
    as the device Config tab's Backup button, just reachable from the
    fleet-wide Backups page too."""
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    try:
        snapshot = backup_service.run_device_config_backup(db, device, current_user.email, label="backups page")
    except backup_service.DeviceBackupError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {
        "device_id": str(device.id),
        "hostname": device.hostname,
        "snapshot_id": str(snapshot.id),
        "version": snapshot.version,
        "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
    }


@router.post("/devices/bulk", status_code=201)
def trigger_fleet_backup(
    payload: FleetConfigBackupRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(_admin_only),
):
    """"Back Up All Devices Now" -- runs a config backup against every
    managed device (or just `device_ids` if given), best-effort per device.
    Returns which hostnames succeeded/failed so the UI can show a real
    summary instead of a single pass/fail toast."""
    device_ids = payload.device_ids if payload else None
    return backup_service.run_fleet_config_backup(db, current_user.email, device_ids=device_ids)


@router.get("/destinations", response_model=list[BackupDestinationRead])
def list_destinations(db: Session = Depends(get_db), _: User = Depends(_admin_only)):
    """Off-site copy targets (S3 / Azure Blob / SFTP) that every future
    successful database backup gets pushed to -- see
    app.services.backup_service and app.services.backup_destination_service.
    """
    destinations = db.query(BackupDestination).order_by(BackupDestination.created_at.desc()).all()
    return [_serialize_destination(d) for d in destinations]


@router.post("/destinations", response_model=BackupDestinationRead, status_code=201)
def create_destination(
    payload: BackupDestinationCreate, db: Session = Depends(get_db), current_user: User = Depends(_admin_only)
):
    if payload.type not in backup_destination_service.DESTINATION_TYPES:
        raise HTTPException(status_code=422, detail=f"Unknown destination type: {payload.type}")

    dest = BackupDestination(
        id=uuid.uuid4(),
        name=payload.name,
        type=payload.type,
        enabled=payload.enabled,
        config_encrypted=backup_destination_service.encrypt_config(payload.type, payload.config),
        created_by=current_user.email,
    )
    db.add(dest)
    db.commit()
    db.refresh(dest)

    audit_service.record_event(
        db, actor=current_user.email, action="Backup Destination Added", result="Success",
        detail=f"{dest.name} ({dest.type})",
    )
    return _serialize_destination(dest)


@router.patch("/destinations/{destination_id}", response_model=BackupDestinationRead)
def update_destination(
    destination_id: str,
    payload: BackupDestinationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(_admin_only),
):
    dest = db.query(BackupDestination).filter(BackupDestination.id == destination_id).first()
    if not dest:
        raise HTTPException(status_code=404, detail="Backup destination not found")

    if payload.name is not None:
        dest.name = payload.name
    if payload.enabled is not None:
        dest.enabled = payload.enabled
    if payload.config is not None:
        # Merge onto the existing decrypted config so, e.g., flipping
        # `enabled` or renaming doesn't force the caller to resend every
        # secret field it never wanted to change.
        existing = backup_destination_service.decrypt_config(dest.config_encrypted)
        existing.update({k: v for k, v in payload.config.items() if v not in (None, "")})
        dest.config_encrypted = backup_destination_service.encrypt_config(dest.type, existing)

    db.commit()
    db.refresh(dest)
    return _serialize_destination(dest)


@router.delete("/destinations/{destination_id}", status_code=204)
def delete_destination(destination_id: str, db: Session = Depends(get_db), current_user: User = Depends(_admin_only)):
    dest = db.query(BackupDestination).filter(BackupDestination.id == destination_id).first()
    if not dest:
        raise HTTPException(status_code=404, detail="Backup destination not found")

    audit_service.record_event(
        db, actor=current_user.email, action="Backup Destination Removed", result="Success",
        detail=f"{dest.name} ({dest.type})",
    )
    db.delete(dest)
    db.commit()


@router.post("/destinations/{destination_id}/test")
def test_destination(destination_id: str, db: Session = Depends(get_db), _: User = Depends(_admin_only)):
    """Connectivity/auth check without uploading anything -- backs the
    "Test" button on the Cloud Destinations panel."""
    dest = db.query(BackupDestination).filter(BackupDestination.id == destination_id).first()
    if not dest:
        raise HTTPException(status_code=404, detail="Backup destination not found")

    config = backup_destination_service.decrypt_config(dest.config_encrypted)
    try:
        backup_destination_service.test_connection(dest.type, config)
    except backup_destination_service.DestinationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@router.get("/{backup_id}/download")
def download_backup(backup_id: str, db: Session = Depends(get_db), _: User = Depends(_admin_only)):
    job = db.query(BackupJob).filter(BackupJob.id == backup_id).first()
    if not job or job.status != "completed" or not job.file_path:
        raise HTTPException(status_code=404, detail="Backup not found or not completed")
    return FileResponse(job.file_path, filename=job.file_path.rsplit("/", 1)[-1], media_type="application/gzip")


@router.delete("/{backup_id}", status_code=204)
def delete_backup(backup_id: str, db: Session = Depends(get_db), _: User = Depends(_admin_only)):
    job = db.query(BackupJob).filter(BackupJob.id == backup_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Backup not found")
    backup_service.delete_backup(db, job)

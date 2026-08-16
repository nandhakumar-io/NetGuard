"""Database Backups: on-demand pg_dump of the NetGuard application
database, with history. See app.services.backup_service for the module
docstring on scope (NetGuard's own DB, not device configs -- those are
app.services.snapshot_service's job).

Restricted to NETWORK_ADMIN throughout -- a backup file is effectively a
full export of every user's data (credentials table included, see
app.models.device's encrypted-at-rest columns), so it gets the same
access bar as credential rotation and device delete.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_roles
from app.models.backup_job import BackupJob
from app.models.user import User, UserRole
from app.services import backup_service

router = APIRouter(prefix="/backups", tags=["backups"])

_admin_only = require_roles(UserRole.NETWORK_ADMIN)


def _serialize(job: BackupJob) -> dict:
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
    }


@router.get("")
def list_backups(db: Session = Depends(get_db), _: User = Depends(_admin_only)):
    """Archive history plus the summary stat-card figures the Backups page
    header shows (total/completed/failed counts, total bytes stored
    locally across completed rows)."""
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

"""Firmware/OS upgrade orchestration.

  GET    /firmware-upgrades              — list (filter by device/batch/status)
  POST   /firmware-upgrades              — create + enqueue a single-device upgrade
  POST   /firmware-upgrades/batch        — create + enqueue the same upgrade across many devices
  GET    /firmware-upgrades/{id}         — single job, with live status/progress
  POST   /firmware-upgrades/{id}/cancel  — cancel before it starts
  POST   /firmware-upgrades/{id}/retry   — re-run a failed/rolled-back job
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.firmware_upgrade import FirmwareUpgrade, FirmwareUpgradeStatus
from app.models.user import User, UserRole
from app.schemas.firmware_upgrade import (
    FirmwareUpgradeBatchCreate,
    FirmwareUpgradeCreate,
    FirmwareUpgradeRead,
)
from app.services import firmware_upgrade_service

router = APIRouter(prefix="/firmware-upgrades", tags=["firmware-upgrades"])

# Pushing a new image + reload onto production devices is squarely a
# Network Admin action -- same bar as approving a change request.
FIRMWARE_MANAGER_ROLES = require_roles(UserRole.NETWORK_ADMIN)


def _enqueue(job_id: uuid.UUID) -> None:
    from app.tasks import run_firmware_upgrade_task

    run_firmware_upgrade_task.delay(str(job_id))


@router.get("", response_model=list[FirmwareUpgradeRead])
def list_jobs(
    device_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = db.query(FirmwareUpgrade)
    if device_id:
        q = q.filter(FirmwareUpgrade.device_id == device_id)
    if batch_id:
        q = q.filter(FirmwareUpgrade.batch_id == batch_id)
    if status:
        q = q.filter(FirmwareUpgrade.status == FirmwareUpgradeStatus(status))
    return q.order_by(desc(FirmwareUpgrade.created_at)).offset(offset).limit(limit).all()


@router.post("", response_model=FirmwareUpgradeRead, status_code=201)
def create_job(
    payload: FirmwareUpgradeCreate,
    db: Session = Depends(get_db),
    user: User = Depends(FIRMWARE_MANAGER_ROLES),
):
    try:
        job = firmware_upgrade_service.create_job(
            db,
            device_id=payload.device_id,
            target_version=payload.target_version,
            image_filename=payload.image_filename,
            image_sha256=payload.image_sha256,
            maintenance_window_id=payload.maintenance_window_id,
            scheduled_at=payload.scheduled_at,
            reboot_wait_seconds=payload.reboot_wait_seconds,
            initiated_by=user.email,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    # Scheduled jobs (future scheduled_at, or gated on a maintenance
    # window that hasn't opened yet) are left for a beat-scheduled sweep
    # to pick up rather than run immediately -- an unscheduled job starts
    # right away, matching how change-request deployment already works.
    if payload.scheduled_at is None:
        _enqueue(job.id)
    return job


@router.post("/batch", response_model=list[FirmwareUpgradeRead], status_code=201)
def create_batch(
    payload: FirmwareUpgradeBatchCreate,
    db: Session = Depends(get_db),
    user: User = Depends(FIRMWARE_MANAGER_ROLES),
):
    if not payload.device_ids:
        raise HTTPException(status_code=400, detail="device_ids must not be empty")

    batch_id = uuid.uuid4()
    jobs = []
    for device_id in payload.device_ids:
        try:
            job = firmware_upgrade_service.create_job(
                db,
                device_id=device_id,
                target_version=payload.target_version,
                image_filename=payload.image_filename,
                image_sha256=payload.image_sha256,
                maintenance_window_id=payload.maintenance_window_id,
                scheduled_at=payload.scheduled_at,
                reboot_wait_seconds=payload.reboot_wait_seconds,
                initiated_by=user.email,
                batch_id=batch_id,
            )
        except ValueError:
            continue  # skip devices that no longer exist rather than failing the whole batch
        jobs.append(job)
        if payload.scheduled_at is None:
            _enqueue(job.id)

    if not jobs:
        raise HTTPException(status_code=404, detail="None of the supplied device_ids were found")
    return jobs


@router.get("/{job_id}", response_model=FirmwareUpgradeRead)
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    job = db.get(FirmwareUpgrade, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Firmware upgrade job not found")
    return job


@router.post("/{job_id}/cancel", response_model=FirmwareUpgradeRead)
def cancel_job(job_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(FIRMWARE_MANAGER_ROLES)):
    job = db.get(FirmwareUpgrade, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Firmware upgrade job not found")
    try:
        return firmware_upgrade_service.cancel_job(db, job, user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{job_id}/retry", response_model=FirmwareUpgradeRead)
def retry_job(job_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(FIRMWARE_MANAGER_ROLES)):
    job = db.get(FirmwareUpgrade, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Firmware upgrade job not found")
    if job.status not in (FirmwareUpgradeStatus.FAILED, FirmwareUpgradeStatus.ROLLED_BACK):
        raise HTTPException(status_code=400, detail=f"Cannot retry a job in status '{job.status.value}'")

    job.status = FirmwareUpgradeStatus.PENDING
    job.error_message = None
    job.current_step_detail = "Retry queued"
    db.commit()
    _enqueue(job.id)
    db.refresh(job)
    return job
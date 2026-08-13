"""Recurring maintenance windows -- "patch Tuesdays", monthly firmware
windows -- defined once instead of created one-off every time.

  GET     /recurring-maintenance-schedules              — list
  POST    /recurring-maintenance-schedules               — create (and immediately generate upcoming windows)
  GET     /recurring-maintenance-schedules/{id}          — single schedule
  PUT     /recurring-maintenance-schedules/{id}           — update (enable/disable, extend/shorten, rename)
  DELETE  /recurring-maintenance-schedules/{id}           — delete (does not delete already-generated windows)
  GET     /recurring-maintenance-schedules/{id}/preview   — preview upcoming occurrence datetimes without saving
  POST    /recurring-maintenance-schedules/{id}/generate  — force a generation pass for this schedule now
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.recurring_maintenance_schedule import RecurringMaintenanceSchedule
from app.models.user import User, UserRole
from app.schemas.recurring_maintenance_schedule import (
    RecurringMaintenanceScheduleCreate,
    RecurringMaintenanceScheduleRead,
    RecurringMaintenanceScheduleUpdate,
    ScheduleOccurrencePreview,
)
from app.services import audit_service, recurring_window_service

router = APIRouter(prefix="/recurring-maintenance-schedules", tags=["recurring-maintenance-schedules"])

MAINTENANCE_MANAGER_ROLES = require_roles(UserRole.NETWORK_ADMIN, UserRole.NETWORK_ENGINEER, UserRole.NOC_ENGINEER)


@router.get("", response_model=list[RecurringMaintenanceScheduleRead])
def list_schedules(
    enabled_only: bool = False,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = db.query(RecurringMaintenanceSchedule)
    if enabled_only:
        q = q.filter(RecurringMaintenanceSchedule.enabled == True)
    return q.order_by(RecurringMaintenanceSchedule.name).all()


@router.post("", response_model=RecurringMaintenanceScheduleRead, status_code=201)
def create_schedule(
    payload: RecurringMaintenanceScheduleCreate,
    db: Session = Depends(get_db),
    user: User = Depends(MAINTENANCE_MANAGER_ROLES),
):
    schedule = RecurringMaintenanceSchedule(
        **payload.model_dump(),
        created_by=user.email,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)

    # Generate immediately so the first window(s) don't wait for the
    # next daily beat tick.
    recurring_window_service.generate_windows(db, schedule)
    db.commit()

    audit_service.record_event(
        db,
        actor=user.email,
        action="recurring_maintenance_schedule_created",
        result="success",
        detail=f"{schedule.name} ({schedule.frequency.value}, every {schedule.interval})",
    )
    return schedule


@router.get("/{schedule_id}", response_model=RecurringMaintenanceScheduleRead)
def get_schedule(schedule_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    schedule = db.get(RecurringMaintenanceSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return schedule


@router.put("/{schedule_id}", response_model=RecurringMaintenanceScheduleRead)
def update_schedule(
    schedule_id: uuid.UUID,
    payload: RecurringMaintenanceScheduleUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(MAINTENANCE_MANAGER_ROLES),
):
    schedule = db.get(RecurringMaintenanceSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(schedule, field, value)
    db.commit()
    db.refresh(schedule)

    if schedule.enabled:
        recurring_window_service.generate_windows(db, schedule)
        db.commit()

    return schedule


@router.delete("/{schedule_id}", status_code=204)
def delete_schedule(
    schedule_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(MAINTENANCE_MANAGER_ROLES),
):
    schedule = db.get(RecurringMaintenanceSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    db.delete(schedule)
    db.commit()
    audit_service.record_event(
        db, actor=user.email, action="recurring_maintenance_schedule_deleted", result="success", detail=schedule.name
    )


@router.get("/{schedule_id}/preview", response_model=ScheduleOccurrencePreview)
def preview_schedule(
    schedule_id: uuid.UUID,
    horizon_days: int = Query(60, ge=1, le=730),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    schedule = db.get(RecurringMaintenanceSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return ScheduleOccurrencePreview(
        occurrences=recurring_window_service.preview_occurrences(schedule, horizon_days=horizon_days)
    )


@router.post("/{schedule_id}/generate")
def force_generate(
    schedule_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(MAINTENANCE_MANAGER_ROLES),
):
    schedule = db.get(RecurringMaintenanceSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    created = recurring_window_service.generate_windows(db, schedule)
    db.commit()
    return {"created": len(created)}

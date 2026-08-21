"""On-Call Schedules CRUD API.

  GET     /on-call-schedules              — list all schedules
  POST    /on-call-schedules              — create a schedule
  PUT     /on-call-schedules/{id}         — update a schedule
  DELETE  /on-call-schedules/{id}         — delete a schedule
  GET     /on-call-schedules/{id}/current — who's on call right now

Self-contained roster feature, not device/tenant-scoped, so any
authenticated user can view it (same trust level as Escalation
Policies, which this exists to feed) -- see
app.api.escalation_policies for the comparable pattern.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.escalation_policy import EscalationPolicy
from app.models.on_call_schedule import OnCallSchedule
from app.models.user import User
from app.schemas.on_call_schedule import (
    OnCallScheduleCreate,
    OnCallScheduleCurrent,
    OnCallScheduleRead,
    OnCallScheduleUpdate,
)
from app.services import on_call_service

router = APIRouter(prefix="/on-call-schedules", tags=["on-call-schedules"])


@router.get("", response_model=list[OnCallScheduleRead])
def list_schedules(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return db.query(OnCallSchedule).order_by(OnCallSchedule.name).all()


@router.post("", response_model=OnCallScheduleRead, status_code=201)
def create_schedule(
    body: OnCallScheduleCreate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    schedule = OnCallSchedule(**body.model_dump())
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


@router.put("/{schedule_id}", response_model=OnCallScheduleRead)
def update_schedule(
    schedule_id: uuid.UUID,
    body: OnCallScheduleUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    schedule = db.get(OnCallSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="On-call schedule not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(schedule, field, value)
    db.commit()
    db.refresh(schedule)
    return schedule


@router.delete("/{schedule_id}", status_code=204)
def delete_schedule(
    schedule_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    schedule = db.get(OnCallSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="On-call schedule not found")
    # Detach rather than block the delete -- an escalation policy that
    # loses its schedule falls back to its own secondary_contacts
    # (same behavior as the FK's ondelete="SET NULL" would give on a
    # raw DB delete; done explicitly here so the ORM session and the
    # response the caller sees agree with what actually happened).
    db.query(EscalationPolicy).filter(EscalationPolicy.on_call_schedule_id == schedule_id).update(
        {EscalationPolicy.on_call_schedule_id: None}
    )
    db.delete(schedule)
    db.commit()


@router.get("/{schedule_id}/current", response_model=OnCallScheduleCurrent)
def get_current_on_call(
    schedule_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    schedule = db.get(OnCallSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="On-call schedule not found")
    contact, is_secondary = on_call_service.current_contact(schedule)
    return OnCallScheduleCurrent(schedule_id=schedule_id, current_contact=contact, is_secondary=is_secondary)

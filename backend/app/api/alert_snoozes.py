"""Alert snooze/mute endpoints.

  POST   /alert-snoozes        — create a snooze (device, category/rule, or both)
  GET    /alert-snoozes        — list snoozes (active-only by default)
  DELETE /alert-snoozes/{id}   — cancel early, un-muting anything it covered
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.device import Device
from app.models.user import User
from app.schemas.alert_snooze import AlertSnoozeCreate, AlertSnoozeRead
from app.services import alert_snooze_service

router = APIRouter(prefix="/alert-snoozes", tags=["alert-snoozes"])


def _to_read(db: Session, snooze) -> AlertSnoozeRead:
    obj = AlertSnoozeRead.model_validate(snooze)
    if snooze.device_id is not None:
        device = db.get(Device, snooze.device_id)
        obj.device_hostname = device.hostname if device else None
    return obj


@router.post("", response_model=AlertSnoozeRead)
def create_snooze(
    payload: AlertSnoozeCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if payload.device_id is not None and db.get(Device, payload.device_id) is None:
        raise HTTPException(status_code=404, detail="Device not found")
    snooze = alert_snooze_service.create_snooze(
        db,
        device_id=payload.device_id,
        category=payload.category,
        expires_at=payload.expires_at,
        reason=payload.reason,
        created_by=user.email,
    )
    return _to_read(db, snooze)


@router.get("", response_model=list[AlertSnoozeRead])
def list_snoozes(
    active_only: bool = Query(True, description="If true (default), only snoozes that haven't expired yet."),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return [_to_read(db, s) for s in alert_snooze_service.list_snoozes(db, active_only=active_only)]


@router.delete("/{snooze_id}")
def cancel_snooze(snooze_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    if not alert_snooze_service.cancel_snooze(db, snooze_id):
        raise HTTPException(status_code=404, detail="Snooze not found")
    return {"cancelled": True}

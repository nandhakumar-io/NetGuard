"""War-room mode -- one click from a critical alert or an open incident
to a single assembled page: affected devices, active change requests,
recent config changes, and a ready-to-post Slack/Teams summary.

  GET   /war-room?alert_id=...        — assemble from a critical alert's correlated group
  GET   /war-room?incident_id=...     — assemble from an already-open incident
  POST  /war-room/post                — post the assembled summary to Slack/Teams
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.services import war_room_service

router = APIRouter(prefix="/war-room", tags=["war-room"])


@router.get("")
def get_war_room(
    alert_id: uuid.UUID | None = Query(None),
    incident_id: uuid.UUID | None = Query(None),
    config_change_lookback_hours: int = Query(24, ge=1, le=168),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    if bool(alert_id) == bool(incident_id):
        raise HTTPException(status_code=400, detail="Provide exactly one of alert_id or incident_id")
    try:
        return war_room_service.assemble_war_room(
            db,
            alert_id=alert_id,
            incident_id=incident_id,
            config_change_lookback_hours=config_change_lookback_hours,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class WarRoomPostRequest(BaseModel):
    alert_id: uuid.UUID | None = None
    incident_id: uuid.UUID | None = None


@router.post("/post", status_code=204)
def post_war_room(
    body: WarRoomPostRequest,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Re-assembles (fresh, not cached) and posts the summary to Slack/
    Teams -- so what gets posted always reflects the current state, even
    if some time passed since the user first opened the war-room view.
    """
    if bool(body.alert_id) == bool(body.incident_id):
        raise HTTPException(status_code=400, detail="Provide exactly one of alert_id or incident_id")
    try:
        war_room = war_room_service.assemble_war_room(db, alert_id=body.alert_id, incident_id=body.incident_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    war_room_service.post_war_room_summary(db, war_room)

"""Incident / postmortem tracking API.

  GET     /incidents                        — list incidents
  POST    /incidents                         — open an incident (from a correlated alert group, or ad hoc)
  GET     /incidents/{id}                    — incident detail + timeline
  PUT     /incidents/{id}                    — update fields / postmortem writeup
  PATCH   /incidents/{id}/status             — move through open -> mitigated -> resolved -> postmortem_due -> closed
  POST    /incidents/{id}/timeline           — add a timeline entry
  GET     /incidents/from-alert/{alert_id}   — preview the correlated alert group an incident would be built from
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.alert import Alert
from app.models.incident import Incident
from app.models.user import User
from app.schemas.alert_runbook import RunbookRef
from app.schemas.incident import (
    IncidentCreate,
    IncidentDetailRead,
    IncidentRead,
    IncidentUpdate,
    TimelineEventCreate,
    TimelineEventRead,
)
from app.services import alert_runbook, incident_service

router = APIRouter(prefix="/incidents", tags=["incidents"])


def _to_read(incident: Incident, db: Session | None = None) -> IncidentRead:
    data = {c.name: getattr(incident, c.name) for c in Incident.__table__.columns}
    data["severity"] = incident.severity.value if hasattr(incident.severity, "value") else incident.severity
    data["status"] = incident.status.value if hasattr(incident.status, "value") else incident.status
    data["alert_ids"] = incident_service.alert_ids_list(incident)
    read = IncidentRead(**data)
    if db is not None and incident.root_cause_alert_id is not None:
        root_alert = db.get(Alert, incident.root_cause_alert_id)
        if root_alert:
            runbook = alert_runbook.resolve_runbook(db, root_alert.category, root_alert.source)
            if runbook:
                read.runbook = RunbookRef(title=runbook.title, url=runbook.url)
    return read


@router.get("", response_model=list[IncidentRead])
def list_incidents(
    status: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = db.query(Incident)
    if status:
        q = q.filter(Incident.status == status)
    incidents = q.order_by(Incident.created_at.desc()).all()
    return [_to_read(i, db) for i in incidents]


@router.get("/from-alert/{alert_id}")
def preview_correlated_group(
    alert_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """What the correlated alert group for `alert_id` looks like right
    now -- the root-cause alert (if this one is a dependent) plus every
    alert it has suppressed -- so the UI can show what an incident opened
    from it would include, before the user commits to creating one."""
    root_alert = db.get(Alert, alert_id)
    if not root_alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    root_id = root_alert.root_cause_alert_id or root_alert.id
    group_ids = incident_service.alert_group_ids(db, root_id)
    group = db.query(Alert).filter(Alert.id.in_(group_ids)).all() if group_ids else [root_alert]
    return {
        "root_cause_alert_id": str(root_id),
        "alert_count": len(group),
        "alerts": [
            {
                "id": str(a.id),
                "severity": a.severity.value if hasattr(a.severity, "value") else a.severity,
                "category": a.category,
                "message": a.message,
                "device_id": str(a.device_id) if a.device_id else None,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in group
        ],
    }


@router.post("", response_model=IncidentRead, status_code=201)
def create_incident(
    body: IncidentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    incident = incident_service.create_incident(
        db,
        title=body.title,
        summary=body.summary,
        severity=body.severity,
        root_cause_alert_id=body.root_cause_alert_id,
        alert_ids=body.alert_ids,
        detected_at=body.detected_at,
        created_by=user.email,
    )
    return _to_read(incident, db)


@router.get("/{incident_id}", response_model=IncidentDetailRead)
def get_incident(
    incident_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    incident = db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    base = _to_read(incident, db)
    return IncidentDetailRead(**base.model_dump(), timeline=[TimelineEventRead.model_validate(e) for e in incident.timeline_events])


@router.put("/{incident_id}", response_model=IncidentRead)
def update_incident(
    incident_id: uuid.UUID,
    body: IncidentUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    incident = db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    updates = body.model_dump(exclude_unset=True)
    new_status = updates.pop("status", None)
    for field, value in updates.items():
        setattr(incident, field, value)
    db.add(incident)
    db.commit()

    if new_status and new_status != (incident.status.value if hasattr(incident.status, "value") else incident.status):
        incident = incident_service.update_status(db, incident, new_status, user.email)
    else:
        db.refresh(incident)
    return _to_read(incident, db)


@router.patch("/{incident_id}/status", response_model=IncidentRead)
def change_status(
    incident_id: uuid.UUID,
    status: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    incident = db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident = incident_service.update_status(db, incident, status, user.email)
    return _to_read(incident, db)


@router.post("/{incident_id}/timeline", response_model=TimelineEventRead, status_code=201)
def add_timeline_event(
    incident_id: uuid.UUID,
    body: TimelineEventCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    incident = db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    event = incident_service.add_timeline_event(
        db, incident, event_type=body.event_type, description=body.description,
        actor=user.email, occurred_at=body.occurred_at,
    )
    return event

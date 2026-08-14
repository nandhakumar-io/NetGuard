import json
import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.alert import Alert
from app.models.incident import Incident, IncidentStatus, IncidentTimelineEvent
from app.services import push_service


def alert_ids_list(incident: Incident) -> List[uuid.UUID]:
    try:
        if not incident.alert_ids:
            return []
        ids_str = json.loads(incident.alert_ids)
        return [uuid.UUID(i) for i in ids_str]
    except (json.JSONDecodeError, ValueError):
        return []


def alert_group_ids(db: Session, root_id: uuid.UUID) -> List[uuid.UUID]:
    alerts = db.query(Alert.id).filter(
        or_(Alert.id == root_id, Alert.root_cause_alert_id == root_id)
    ).all()
    return [a.id for a in alerts]


def add_timeline_event(
    db: Session,
    incident: Incident,
    event_type: str,
    description: str,
    actor: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
) -> IncidentTimelineEvent:
    event = IncidentTimelineEvent(
        incident_id=incident.id,
        event_type=event_type,
        description=description,
        actor=actor,
        occurred_at=occurred_at,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    # Refresh incident to load new events
    db.refresh(incident)
    return event


def create_incident(
    db: Session,
    title: str,
    summary: Optional[str],
    severity: str,
    root_cause_alert_id: Optional[uuid.UUID],
    alert_ids: Optional[List[uuid.UUID]],
    detected_at: Optional[datetime],
    created_by: str,
) -> Incident:
    if alert_ids is None:
        alert_ids = []

    alert_ids_json = json.dumps([str(aid) for aid in alert_ids])

    incident = Incident(
        title=title,
        summary=summary,
        severity=severity,
        root_cause_alert_id=root_cause_alert_id,
        alert_ids=alert_ids_json,
        detected_at=detected_at,
        created_by=created_by,
        status=IncidentStatus.OPEN,
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)

    add_timeline_event(
        db=db,
        incident=incident,
        event_type="note",
        description="Incident created.",
        actor=created_by,
        occurred_at=detected_at or datetime.utcnow(),
    )

    # P1 (CRITICAL) incidents get pushed straight to every on-call phone,
    # on top of whatever Slack/Teams/email fan-out already happened for
    # the underlying alerts -- an incident is the "this is now a real
    # outage" signal, distinct from (and rarer than) individual alerts,
    # so it's the right trigger point for a wake-someone-up push rather
    # than pushing on every critical alert. Best-effort: a push failure
    # never blocks incident creation.
    if severity == "critical":
        push_service.send_push(
            db,
            title=f"🚨 P1 Incident: {title}",
            message=summary or "Critical incident opened in NetGuard. Tap to view details.",
            severity="critical",
        )

    return incident


def update_status(db: Session, incident: Incident, new_status: str, user_email: str) -> Incident:
    old_status = incident.status.value if hasattr(incident.status, "value") else incident.status
    if not isinstance(new_status, IncidentStatus):
        new_status = IncidentStatus(new_status)
    incident.status = new_status

    if new_status == IncidentStatus.MITIGATED and not incident.mitigated_at:
        incident.mitigated_at = datetime.utcnow()
    elif new_status == IncidentStatus.RESOLVED and not incident.resolved_at:
        incident.resolved_at = datetime.utcnow()
    elif new_status == IncidentStatus.CLOSED and not incident.closed_at:
        incident.closed_at = datetime.utcnow()

    db.add(incident)
    db.commit()
    db.refresh(incident)

    add_timeline_event(
        db,
        incident=incident,
        event_type="status_change",
        description=f"Status changed from {old_status} to {new_status.value}",
        actor=user_email,
        occurred_at=datetime.utcnow(),
    )
    return incident

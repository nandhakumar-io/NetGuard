"""Incident / postmortem tracking.

Bridges app.services.alert_correlation_service's live "root cause +
suppressed dependents" alert grouping into a durable Incident record once
the group is worth writing up -- see app.models.incident for the full
rationale.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy.orm import Session

from app.models.alert import Alert
from app.models.incident import Incident, IncidentTimelineEvent
from app.services import audit_service


def alert_group_ids(db: Session, root_cause_alert_id: uuid.UUID | None) -> list[uuid.UUID]:
    """Root-cause alert id plus every alert currently pointing at it via
    `root_cause_alert_id` (the live correlated group, per
    alert_correlation_service)."""
    if not root_cause_alert_id:
        return []
    dependents = (
        db.query(Alert.id).filter(Alert.root_cause_alert_id == root_cause_alert_id).all()
    )
    return [root_cause_alert_id, *[d[0] for d in dependents]]


def create_incident(
    db: Session,
    *,
    title: str,
    summary: str | None,
    severity: str,
    root_cause_alert_id: uuid.UUID | None,
    alert_ids: list[uuid.UUID] | None,
    detected_at,
    created_by: str,
) -> Incident:
    resolved_alert_ids = alert_ids or alert_group_ids(db, root_cause_alert_id)

    # Detection defaults to the earliest `created_at` among the folded-in
    # alerts, if not given explicitly -- that's when the underlying
    # condition was actually first observed, not when the incident record
    # was opened (which is often well after the fact, for the retro).
    if detected_at is None and resolved_alert_ids:
        earliest = (
            db.query(Alert.created_at)
            .filter(Alert.id.in_(resolved_alert_ids))
            .order_by(Alert.created_at.asc())
            .first()
        )
        detected_at = earliest[0] if earliest else None

    incident = Incident(
        title=title,
        summary=summary,
        severity=severity,
        root_cause_alert_id=root_cause_alert_id,
        alert_ids=json.dumps([str(a) for a in resolved_alert_ids]),
        detected_at=detected_at,
        created_by=created_by,
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)

    add_timeline_event(
        db, incident,
        event_type="detection",
        description=f"Incident opened from {len(resolved_alert_ids)} correlated alert(s)."
        if resolved_alert_ids else "Incident opened.",
        actor=created_by,
        occurred_at=detected_at,
    )

    audit_service.record_event(
        db, actor=created_by, action="Incident Opened", result=title,
        detail=f"incident_id={incident.id} alert_count={len(resolved_alert_ids)}",
    )
    return incident


def add_timeline_event(
    db: Session, incident: Incident, *, event_type: str, description: str,
    actor: str | None, occurred_at=None,
) -> IncidentTimelineEvent:
    event = IncidentTimelineEvent(
        incident_id=incident.id,
        event_type=event_type,
        description=description,
        actor=actor,
    )
    if occurred_at is not None:
        event.occurred_at = occurred_at
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def alert_ids_list(incident: Incident) -> list[str]:
    try:
        return json.loads(incident.alert_ids or "[]")
    except (json.JSONDecodeError, TypeError):
        return []


def update_status(db: Session, incident: Incident, new_status: str, actor: str) -> Incident:
    from datetime import datetime, timezone

    old_status = incident.status.value if hasattr(incident.status, "value") else incident.status
    incident.status = new_status
    now = datetime.now(timezone.utc)
    if new_status == "mitigated" and not incident.mitigated_at:
        incident.mitigated_at = now
    elif new_status == "resolved" and not incident.resolved_at:
        incident.resolved_at = now
    elif new_status == "closed" and not incident.closed_at:
        incident.closed_at = now
    db.add(incident)
    add_timeline_event(
        db, incident, event_type="status_change",
        description=f"Status changed: {old_status} → {new_status}", actor=actor,
    )
    db.commit()
    db.refresh(incident)
    return incident

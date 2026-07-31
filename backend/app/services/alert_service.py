"""Alert service — create, acknowledge, resolve, and summarize alerts.

Centralises alert persistence so SNMP threshold breaches, inbound traps,
drift detections, and protocol failures all flow through a single path
that:
  1. Creates/persists the Alert row.
  2. Publishes a lightweight event on the Redis ALERTS_CHANNEL so every
     connected WebSocket (Alert Center, Dashboard) updates instantly.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.alert import Alert, AlertSeverity, AlertSource
from app.services import event_bus


# ------------------------------------------------------------------
# Create
# ------------------------------------------------------------------
def create_alert(
    db: Session,
    *,
    device_id: uuid.UUID | None,
    severity: str | AlertSeverity,
    source: str | AlertSource,
    category: str,
    message: str,
) -> Alert:
    """Persist a new alert and publish a realtime event."""
    if isinstance(severity, str):
        severity = AlertSeverity(severity)
    if isinstance(source, str):
        source = AlertSource(source)

    alert = Alert(
        device_id=device_id,
        severity=severity,
        source=source,
        category=category,
        message=message,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    event_bus.publish_event(
        "alert_created",
        alert_id=str(alert.id),
        severity=severity.value,
        category=category,
        channel=event_bus.ALERTS_CHANNEL,
    )
    # Also nudge dashboard so the summary stat cards refresh.
    event_bus.publish_event("alert_created", alert_id=str(alert.id))

    return alert


# ------------------------------------------------------------------
# Acknowledge
# ------------------------------------------------------------------
def acknowledge_alert(db: Session, alert_id: uuid.UUID, user_email: str) -> Alert:
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise ValueError("Alert not found")
    alert.acknowledged = True
    alert.acknowledged_by = user_email
    db.commit()
    db.refresh(alert)

    event_bus.publish_event(
        "alert_acknowledged",
        alert_id=str(alert.id),
        channel=event_bus.ALERTS_CHANNEL,
    )
    return alert


# ------------------------------------------------------------------
# Resolve
# ------------------------------------------------------------------
def resolve_alert(db: Session, alert_id: uuid.UUID, user_email: str) -> Alert:
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise ValueError("Alert not found")
    alert.resolved = True
    alert.resolved_at = datetime.now(timezone.utc)
    alert.resolved_by = user_email
    if not alert.acknowledged:
        alert.acknowledged = True
        alert.acknowledged_by = user_email
    db.commit()
    db.refresh(alert)

    event_bus.publish_event(
        "alert_resolved",
        alert_id=str(alert.id),
        channel=event_bus.ALERTS_CHANNEL,
    )
    event_bus.publish_event("alert_resolved", alert_id=str(alert.id))

    return alert


# ------------------------------------------------------------------
# Summary (for dashboard cards)
# ------------------------------------------------------------------
def get_alert_summary(db: Session) -> dict:
    """Returns counts of active (non-resolved) alerts grouped by severity."""
    base = db.query(Alert).filter(Alert.resolved == False)  # noqa: E712
    critical = base.filter(Alert.severity == AlertSeverity.CRITICAL).count()
    warning = base.filter(Alert.severity == AlertSeverity.WARNING).count()
    info = base.filter(Alert.severity == AlertSeverity.INFO).count()
    total_resolved = db.query(Alert).filter(Alert.resolved == True).count()  # noqa: E712

    return {
        "critical": critical,
        "warning": warning,
        "info": info,
        "active_total": critical + warning + info,
        "resolved": total_resolved,
    }

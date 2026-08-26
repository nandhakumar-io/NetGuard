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
from app.services import (
    alert_correlation_service,
    alert_snooze_service,
    event_bus,
    maintenance_window_service,
)


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

    window = maintenance_window_service.find_active_window(db, device_id)
    snooze = alert_snooze_service.find_active_snooze(db, device_id, category)

    alert = Alert(
        device_id=device_id,
        severity=severity,
        source=source,
        category=category,
        message=message,
        suppressed_by_window_id=window.id if window else None,
        muted_by_snooze_id=snooze.id if snooze else None,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    # A window-suppressed or snoozed alert is still persisted
    # (queryable/auditable after the fact) but doesn't fan out
    # realtime/notification events -- that's the whole point of both
    # scheduling planned work and muting a known-noisy rule, so neither
    # pages on-call or lights up the Alert Center.
    if window is None and snooze is None:
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


# Severity ranks so raise_alert() can escalate (warning -> critical) a
# standing alert without ever silently de-escalating one (a momentarily
# better reading shouldn't hide a still-unresolved critical condition).
_SEVERITY_RANK = {AlertSeverity.INFO: 0, AlertSeverity.WARNING: 1, AlertSeverity.CRITICAL: 2}


# ------------------------------------------------------------------
# Raise (dedup-aware) -- the entry point poll/check/drift-scan code
# should actually call, instead of constructing Alert(...) directly.
# ------------------------------------------------------------------
def raise_alert(
    db: Session,
    *,
    device_id: uuid.UUID | None,
    severity: str | AlertSeverity,
    source: str | AlertSource,
    category: str,
    message: str,
) -> tuple[Alert, bool]:
    """Raises an alert for a device+category, updating a standing one in
    place instead of inserting a duplicate row every time the same
    condition is found again.

    Before this existed, every health poll / protocol failure / drift
    scan that found the same still-active problem (e.g. persistent high
    CPU) constructed a brand-new Alert row unconditionally -- so "Clear
    Alerts" appeared broken: the very next poll cycle would immediately
    recreate a fresh duplicate for a condition that hadn't actually gone
    away, making it look like clearing had no effect.

    Now: looks for an existing *unresolved* alert with the same
    device_id + category.
      - found: bumps `last_seen_at` and `occurrence_count`, refreshes the
        message, and escalates `severity` if the new reading is worse
        (never de-escalates). No new row, no new notification spam.
      - not found (first occurrence, or the previous one was already
        resolved/cleared): inserts a new Alert row, same as create_alert().

    Returns (alert, is_new_or_reopened) so callers can decide whether to
    fan out a Slack/Teams notification only on first occurrence rather
    than every single poll.
    """
    if isinstance(severity, str):
        severity = AlertSeverity(severity)
    if isinstance(source, str):
        source = AlertSource(source)

    now = datetime.now(timezone.utc)
    window = maintenance_window_service.find_active_window(db, device_id, now=now)
    snooze = alert_snooze_service.find_active_snooze(db, device_id, category, now=now)

    existing = (
        db.query(Alert)
        .filter(
            Alert.device_id == device_id,
            Alert.category == category,
            Alert.resolved == False,
        )
        .order_by(Alert.created_at.desc())
        .first()
    )

    if existing is not None:
        existing.message = message
        existing.last_seen_at = now
        existing.occurrence_count = (existing.occurrence_count or 1) + 1
        if _SEVERITY_RANK[severity] > _SEVERITY_RANK[existing.severity]:
            existing.severity = severity
        # Attribute suppression the moment a window opens over an
        # already-standing alert too (don't require a brand new row to
        # pick it up), and clear it again once the window has passed --
        # so an alert that outlives its maintenance window naturally
        # reappears as active instead of staying silently suppressed.
        existing.suppressed_by_window_id = window.id if window else None
        # Same live-reattribution as the window check above: a snooze
        # created *after* this alert was already standing should mute it
        # immediately, and one that lapses should naturally stop hiding
        # it the next time this alert is touched.
        existing.muted_by_snooze_id = snooze.id if snooze else None
        db.commit()
        db.refresh(existing)

        if window is None and snooze is None:
            event_bus.publish_event(
                "alert_updated",
                alert_id=str(existing.id),
                severity=existing.severity.value,
                category=category,
                channel=event_bus.ALERTS_CHANNEL,
            )
            alert_correlation_service.correlate_downstream(db, existing)
        return existing, False

    alert = Alert(
        device_id=device_id,
        severity=severity,
        source=source,
        category=category,
        message=message,
        last_seen_at=now,
        occurrence_count=1,
        suppressed_by_window_id=window.id if window else None,
        muted_by_snooze_id=snooze.id if snooze else None,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    if window is None and snooze is None:
        event_bus.publish_event(
            "alert_created",
            alert_id=str(alert.id),
            severity=severity.value,
            category=category,
            channel=event_bus.ALERTS_CHANNEL,
        )
        event_bus.publish_event("alert_created", alert_id=str(alert.id))

    alert_correlation_service.correlate_downstream(db, alert)

    return alert, True


# ------------------------------------------------------------------
# Auto-resolve (system-clears, not user-clears) -- the counterpart to
# raise_alert() for conditions that recover on their own between polls
# (an interface coming back up, a device answering ping again). Without
# this, a standing alert for a condition that genuinely cleared just sat
# in "Active Alerts" forever until a human clicked Resolve, which is
# misleading on a NOC dashboard that's supposed to reflect current state.
# ------------------------------------------------------------------
def auto_resolve(
    db: Session,
    *,
    device_id: uuid.UUID | None,
    category: str,
    note: str | None = None,
) -> Alert | None:
    """Resolves the standing unresolved alert (if any) for device_id +
    category, attributed to "system" rather than a human user. Returns
    the resolved Alert, or None if there was nothing active to clear.
    """
    existing = (
        db.query(Alert)
        .filter(
            Alert.device_id == device_id,
            Alert.category == category,
            Alert.resolved == False,
        )
        .order_by(Alert.created_at.desc())
        .first()
    )
    if existing is None:
        return None

    existing.resolved = True
    existing.resolved_at = datetime.now(timezone.utc)
    existing.resolved_by = "system"
    if note:
        existing.message = note
    if not existing.acknowledged:
        existing.acknowledged = True
        existing.acknowledged_by = "system"
    db.commit()
    db.refresh(existing)

    event_bus.publish_event(
        "alert_resolved",
        alert_id=str(existing.id),
        channel=event_bus.ALERTS_CHANNEL,
    )
    event_bus.publish_event("alert_resolved", alert_id=str(existing.id))

    alert_correlation_service.release_suppressed(db, existing)

    return existing


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
# Escalate
# ------------------------------------------------------------------
def escalate_alert(db: Session, alert_id: uuid.UUID) -> Alert:
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise ValueError("Alert not found")
    alert.escalated = True
    alert.escalation_count += 1
    now = datetime.now(timezone.utc)
    if not alert.escalated_at:
        alert.escalated_at = now
    alert.last_escalated_at = now
    db.commit()
    db.refresh(alert)

    event_bus.publish_event(
        "alert_escalated",
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

    # If this was a root cause other alerts were suppressed under, they're
    # independent conditions again now that it's resolved.
    alert_correlation_service.release_suppressed(db, alert)

    return alert


# ------------------------------------------------------------------
# Purge (hard delete)
# ------------------------------------------------------------------
def purge_alerts(db: Session, *, device_id: uuid.UUID | None = None, only_active: bool = False) -> int:
    """Permanently remove alert rows -- backs the 'Clear Alerts' button when
    the operator wants them gone from the list entirely, not just marked
    resolved. Unlike clear_alerts(), this does not preserve an audit trail
    for the removed rows (they're deleted), so it's a distinct, explicit
    action from acknowledge/resolve.

    Returns the number of alerts deleted.
    """
    q = db.query(Alert)
    if device_id is not None:
        q = q.filter(Alert.device_id == device_id)
    if only_active:
        q = q.filter(Alert.resolved == False)

    alert_ids = [a.id for a in q.all()]
    if not alert_ids:
        return 0

    # Detach anything suppressed under one of the alerts being deleted so
    # it doesn't reference a root_cause_alert_id that no longer exists.
    db.query(Alert).filter(Alert.root_cause_alert_id.in_(alert_ids)).update(
        {"suppressed": False, "root_cause_alert_id": None}, synchronize_session=False
    )
    deleted = q.delete(synchronize_session=False)
    db.commit()

    event_bus.publish_event(
        "alerts_purged",
        count=deleted,
        device_id=str(device_id) if device_id else None,
        channel=event_bus.ALERTS_CHANNEL,
    )
    return deleted


# ------------------------------------------------------------------
# Clear (bulk resolve)
# ------------------------------------------------------------------
def clear_alerts(db: Session, user_email: str, *, device_id: uuid.UUID | None = None) -> int:
    """Resolve every currently-active (unresolved) alert in one shot --
    backs the 'Clear Alerts' button on the Alert Center / device Alerts
    tab. Alerts are resolved rather than hard-deleted so the audit trail
    (who cleared what, when) is preserved, same rationale as
    resolve_alert() above.

    Returns the number of alerts cleared.
    """
    q = db.query(Alert).filter(Alert.resolved == False)
    if device_id is not None:
        q = q.filter(Alert.device_id == device_id)

    now = datetime.now(timezone.utc)
    alert_ids = [a.id for a in q.all()]
    if not alert_ids:
        return 0

    q.update(
        {
            "resolved": True,
            "resolved_at": now,
            "resolved_by": user_email,
            "acknowledged": True,
            "acknowledged_by": func.coalesce(Alert.acknowledged_by, user_email),
        },
        synchronize_session=False,
    )
    db.commit()

    # Release anything suppressed under an alert that just got bulk-resolved.
    db.query(Alert).filter(Alert.root_cause_alert_id.in_(alert_ids)).update(
        {"suppressed": False, "root_cause_alert_id": None}, synchronize_session=False
    )
    db.commit()

    event_bus.publish_event(
        "alerts_cleared",
        count=len(alert_ids),
        device_id=str(device_id) if device_id else None,
        channel=event_bus.ALERTS_CHANNEL,
    )
    event_bus.publish_event("alerts_cleared", count=len(alert_ids))

    return len(alert_ids)


# ------------------------------------------------------------------
# Summary (for dashboard cards)
# ------------------------------------------------------------------
def get_alert_summary(db: Session) -> dict:
    """Returns counts of active (non-resolved) alerts grouped by severity."""
    base = db.query(Alert).filter(Alert.resolved == False)
    critical = base.filter(Alert.severity == AlertSeverity.CRITICAL).count()
    warning = base.filter(Alert.severity == AlertSeverity.WARNING).count()
    info = base.filter(Alert.severity == AlertSeverity.INFO).count()
    total_resolved = db.query(Alert).filter(Alert.resolved == True).count()

    return {
        "critical": critical,
        "warning": warning,
        "info": info,
        "active_total": critical + warning + info,
        "resolved": total_resolved,
    }

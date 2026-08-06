"""Per-device / per-rule alert snoozing with a mandatory expiry.

Three independent ways an Alert can end up hidden from the default Active
Alerts view now exist on the model -- this module owns the newest one:
  - suppressed_by_window_id  (maintenance_window_service) -- planned work
  - suppressed / root_cause_alert_id (alert_correlation_service) -- automatic,
    topology-inferred "this is a consequence of that other failure"
  - muted_by_snooze_id (here) -- a human explicitly asked to stop being
    reminded about a device or rule for a bounded window of time

Snoozes are deliberately expiry-only (no "snooze forever") -- an
indefinite mute is how real incidents quietly stop paging anyone and
nobody notices until it matters. Forcing an expiry means the worst case
is "I get reminded again sooner than I'd like", never "this alert
category silently went dark forever."
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.alert import Alert
from app.models.alert_snooze import AlertSnooze


def create_snooze(
    db: Session,
    *,
    device_id: uuid.UUID | None,
    category: str | None,
    expires_at: datetime,
    reason: str | None,
    created_by: str,
) -> AlertSnooze:
    """Creates the snooze and immediately mutes any currently-active
    alert it already covers -- mirrors maintenance_window_service's
    "attribute suppression the moment a window opens over an
    already-standing alert" behavior, so muting doesn't wait for the next
    poll cycle to take visible effect.
    """
    snooze = AlertSnooze(
        device_id=device_id,
        category=category,
        expires_at=expires_at,
        reason=reason,
        created_by=created_by,
    )
    db.add(snooze)
    db.commit()
    db.refresh(snooze)

    q = db.query(Alert).filter(Alert.resolved == False)
    if device_id is not None and category is not None:
        q = q.filter(Alert.device_id == device_id, Alert.category == category)
    elif device_id is not None:
        q = q.filter(Alert.device_id == device_id)
    else:
        q = q.filter(Alert.category == category)
    for alert in q.all():
        alert.muted_by_snooze_id = snooze.id
    db.commit()

    return snooze


def cancel_snooze(db: Session, snooze_id: uuid.UUID) -> bool:
    """Ends a snooze early: deletes the row and un-mutes anything
    currently pointing at it (so cancelling flips the affected alerts
    straight back to visible instead of waiting for expires_at)."""
    snooze = db.get(AlertSnooze, snooze_id)
    if snooze is None:
        return False
    db.query(Alert).filter(Alert.muted_by_snooze_id == snooze_id).update({Alert.muted_by_snooze_id: None})
    db.delete(snooze)
    db.commit()
    return True


def find_active_snooze(db: Session, device_id: uuid.UUID | None, category: str | None, *, now: datetime | None = None) -> AlertSnooze | None:
    """Used by alert_service.raise_alert/create_alert at alert-creation
    time -- checks device-specific, category/rule-wide, and combined
    snoozes, most specific (device+category) first.
    """
    now = now or datetime.now(timezone.utc)
    q = db.query(AlertSnooze).filter(AlertSnooze.expires_at > now)
    q = q.filter(
        or_(
            and_(AlertSnooze.device_id == device_id, AlertSnooze.category == category),
            and_(AlertSnooze.device_id == device_id, AlertSnooze.category.is_(None)),
            and_(AlertSnooze.device_id.is_(None), AlertSnooze.category == category),
        )
    )
    return q.order_by(AlertSnooze.expires_at.desc()).first()


def active_mute_map(db: Session, alerts: list[Alert], *, now: datetime | None = None) -> dict[uuid.UUID, datetime]:
    """Given a page of Alert rows, returns {alert.id: snooze.expires_at}
    for only the ones whose muted_by_snooze_id points at a snooze that
    hasn't expired yet -- lets the API report a live "still muted until"
    without a background job continuously clearing muted_by_snooze_id the
    instant a snooze lapses (see AlertRead.muted_until's docstring: the
    FK is left in place as history either way).
    """
    now = now or datetime.now(timezone.utc)
    snooze_ids = {a.muted_by_snooze_id for a in alerts if a.muted_by_snooze_id is not None}
    if not snooze_ids:
        return {}
    rows = db.query(AlertSnooze.id, AlertSnooze.expires_at).filter(
        AlertSnooze.id.in_(snooze_ids), AlertSnooze.expires_at > now
    ).all()
    expiry_by_snooze = {row.id: row.expires_at for row in rows}
    return {
        a.id: expiry_by_snooze[a.muted_by_snooze_id]
        for a in alerts
        if a.muted_by_snooze_id is not None and a.muted_by_snooze_id in expiry_by_snooze
    }


def list_snoozes(db: Session, *, active_only: bool = True) -> list[AlertSnooze]:
    q = db.query(AlertSnooze)
    if active_only:
        q = q.filter(AlertSnooze.expires_at > datetime.now(timezone.utc))
    return q.order_by(AlertSnooze.expires_at.asc()).all()

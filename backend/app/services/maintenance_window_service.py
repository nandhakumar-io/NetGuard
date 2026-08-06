"""Maintenance windows -- lookups used by app.services.alert_service to
decide whether a newly-raised alert should be suppressed (still stored,
just not paged/shown as active) because the device is inside planned,
approved work.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.device import Device
from app.models.maintenance_window import MaintenanceScope, MaintenanceWindow


def find_active_window(db: Session, device_id: uuid.UUID | None, *, now: datetime | None = None) -> MaintenanceWindow | None:
    """Returns the active (started, not yet ended, not cancelled) window
    that covers `device_id`, if any. Checked on every alert raise, so this
    stays a single indexed query rather than loading all windows.

    Precedence when multiple windows could match (e.g. a FLEET window and
    a DEVICE-scoped one both active): whichever started most recently
    wins for the purpose of attribution -- doesn't change the outcome
    (alert is suppressed either way), just which window id gets recorded.
    """
    if device_id is None:
        return None

    now = now or datetime.now(timezone.utc)

    device = db.query(Device).filter(Device.id == device_id).first()
    site = device.site if device else None

    query = db.query(MaintenanceWindow).filter(
        MaintenanceWindow.cancelled == False,
        MaintenanceWindow.starts_at <= now,
        MaintenanceWindow.ends_at >= now,
    )

    scope_filters = [MaintenanceWindow.scope == MaintenanceScope.FLEET]
    scope_filters.append(
        (MaintenanceWindow.scope == MaintenanceScope.DEVICE) & (MaintenanceWindow.device_id == device_id)
    )
    if site:
        scope_filters.append((MaintenanceWindow.scope == MaintenanceScope.SITE) & (MaintenanceWindow.site == site))

    query = query.filter(or_(*scope_filters)).order_by(MaintenanceWindow.starts_at.desc())
    return query.first()


def is_device_in_maintenance(db: Session, device_id: uuid.UUID | None, *, now: datetime | None = None) -> bool:
    return find_active_window(db, device_id, now=now) is not None

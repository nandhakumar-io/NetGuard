"""Maintenance windows -- lookups used by app.services.alert_service to
decide whether a newly-raised alert should be suppressed (still stored,
just not paged/shown as active) because the device is inside planned,
approved work.
"""
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.device import Device
from app.models.maintenance_window import MaintenanceScope, MaintenanceWindow

if TYPE_CHECKING:
    from app.models.change_request import ChangeRequest


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


def sync_for_change_request(
    db: Session, cr: "ChangeRequest", device_ids: list[uuid.UUID], *, actor_email: str
) -> list[MaintenanceWindow]:
    """Auto-creates one DEVICE-scoped MaintenanceWindow per target device
    covering an approved change request's declared maintenance_window_
    start/end, tagged `change_request_id=cr.id`.

    Without this, the two "maintenance window" concepts in the schema
    never touched each other: ChangeRequest.maintenance_window_start/end
    only gated *when the deployment task fires* (see api.change_requests.
    approve_change_request), while alert_service only ever checked the
    separate MaintenanceWindow table to decide suppression. A device
    being deployed to during its own approved change's declared window
    still paged NOC for the alert noise that change caused.

    No-op if the change request didn't declare a window (not every shop
    uses one -- same "deploys immediately" behavior as before applies to
    suppression too: nothing to suppress against). Idempotent per device:
    re-approval flows / retries won't pile up duplicate windows for the
    same change request.
    """
    if cr.maintenance_window_start is None or cr.maintenance_window_end is None:
        return []

    existing_device_ids = {
        w.device_id
        for w in db.query(MaintenanceWindow.device_id)
        .filter(MaintenanceWindow.change_request_id == cr.id, MaintenanceWindow.cancelled == False)
        .all()
    }

    created: list[MaintenanceWindow] = []
    for device_id in device_ids:
        if device_id in existing_device_ids:
            continue
        window = MaintenanceWindow(
            id=uuid.uuid4(),
            name=f"Change request {cr.id} deployment window",
            reason=f"Auto-created on approval of change request {cr.id} so its own deployment "
                    "doesn't page NOC for alerts caused by the planned change.",
            scope=MaintenanceScope.DEVICE,
            device_id=device_id,
            starts_at=cr.maintenance_window_start,
            ends_at=cr.maintenance_window_end,
            change_request_id=cr.id,
            created_by=f"change-request:{actor_email}",
        )
        db.add(window)
        created.append(window)
    if created:
        db.commit()
    return created


def cancel_for_change_request(db: Session, change_request_id: uuid.UUID, *, reason: str, actor_email: str) -> int:
    """Cancels (does not delete -- keeps the audit trail, same as manual
    cancellation) any still-active windows this change request auto-
    created. Called when a change ends up FAILED or ROLLED_BACK: the
    device didn't end up in the planned state, so it should go back to
    paging normally instead of staying silently suppressed for the rest
    of a window that no longer reflects what's actually happening on it.
    """
    now = datetime.now(timezone.utc)
    rows = (
        db.query(MaintenanceWindow)
        .filter(MaintenanceWindow.change_request_id == change_request_id, MaintenanceWindow.cancelled == False)
        .all()
    )
    for row in rows:
        row.cancelled = True
        row.cancelled_at = now
        row.cancelled_by = actor_email
        row.reason = f"{row.reason or ''}\n\nAuto-cancelled: {reason}".strip()
    if rows:
        db.commit()
    return len(rows)

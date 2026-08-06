import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class AlertSnooze(Base):
    """A user-created, time-boxed mute -- "stop paging me about this" for
    either a single device, an entire alert category/rule (e.g. every
    "High CPU" alert fleet-wide), or the intersection of both. Distinct
    from the other two suppression mechanisms already on Alert:
    `suppressed_by_window_id` (a maintenance window covering a device) is
    scheduled/planned and time-bound to the window; `suppressed` +
    `root_cause_alert_id` (app.services.alert_correlation_service) is
    automatic topology-based "this is a consequence of that other
    failure". A snooze is neither -- it's "I know about this, stop
    reminding me for N hours", always has an explicit `expires_at` (no
    indefinite mutes -- see alert_snooze_service's rationale), and is
    reversible early via DELETE /alert-snoozes/{id}.

    At least one of device_id/category must be set (enforced in
    app.schemas.alert_snooze.AlertSnoozeCreate, not here) -- device_id
    only = mute everything on that device, category only = mute that
    rule everywhere, both = mute just that device+category pair.
    """

    __tablename__ = "alert_snoozes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True, index=True)
    category = Column(String, nullable=True, index=True)
    reason = Column(String, nullable=True)

    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)

    created_by = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
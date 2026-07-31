import enum
import uuid

from sqlalchemy import Boolean, Column, DateTime, Enum, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class NotificationSeverity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class NotificationEventType(str, enum.Enum):
    """Coarse category used to pick an email template (see
    app.services.notification_service._TEMPLATES) and to let the frontend
    render a distinct icon/label per event. `GENERIC` is the fallback for
    any notify() call that doesn't match a known pattern.
    """

    DEPLOYMENT_SUCCEEDED = "deployment_succeeded"
    DEPLOYMENT_FAILED = "deployment_failed"
    ROLLBACK_TRIGGERED = "rollback_triggered"
    DRIFT_HIGH = "drift_high"
    DRIFT_CRITICAL = "drift_critical"
    GENERIC = "generic"


class Notification(Base):
    """In-app Notification Center record (SRS FR-11).

    One row is written for every `notification_service.notify(...)` call
    (deploy success/fail, rollback, drift, threshold alerts, etc.) alongside
    the existing Slack/Teams/Email fan-out, and pushed live to any connected
    `/notifications/ws` client via app.services.event_bus.NOTIFICATIONS_CHANNEL.
    Independent of the `Alert` table -- Alerts are device-health records with
    acknowledge/resolve workflow; Notifications are a flat activity feed of
    every notify() event with a simple read/unread flag.
    """

    __tablename__ = "notifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    event_type = Column(Enum(NotificationEventType), nullable=False, default=NotificationEventType.GENERIC)
    severity = Column(Enum(NotificationSeverity), nullable=False, default=NotificationSeverity.INFO)

    title = Column(String, nullable=False)
    message = Column(Text, nullable=False)

    device_hostname = Column(String, nullable=True)
    change_request_id = Column(UUID(as_uuid=True), nullable=True)
    deployment_id = Column(UUID(as_uuid=True), nullable=True)

    read = Column(Boolean, nullable=False, default=False, server_default="false")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
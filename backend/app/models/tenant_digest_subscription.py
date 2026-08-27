"""Per-tenant Alert/Incident/AuditLog digest subscriptions.

Same overall shape as app.models.change_request_digest (build -> render
-> email) but generalized in two ways that digest didn't need to be:

  1. Per-tenant rather than one fixed operator-wide schedule -- an MSP
     managing several tenants wants each customer's rollup delivered on
     its own cadence/recipients, not one email blending every tenant's
     activity together.
  2. Driven by a DB row per subscription instead of one static Celery
     beat crontab entry -- "daily at 8am" vs "weekly on Monday at 9am",
     a different recipient list per tenant, etc. can't be expressed as a
     fixed set of crontab schedules without one beat entry per tenant/
     cadence combination that has to be kept in sync by hand as tenants
     are added. Instead app.tasks.run_tenant_digest_dispatch_task runs
     hourly and this table is the source of truth for who's due --
     see app.services.tenant_digest_service.subscriptions_due_at.

Alert/Incident/AuditLog rows are read directly (via Device.tenant_id for
Alert -- see tenant_digest_service._device_ids_for_tenant) rather than
introducing a new tenant-scoped read path; nothing here changes how
those tables are written.
"""
import enum
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.database import Base


class DigestCadence(str, enum.Enum):
    DAILY = "daily"
    WEEKLY = "weekly"


class DigestSeverityFloor(str, enum.Enum):
    """The lowest severity that is still emailed live (via
    notification_service.notify) as it happens. Anything below this
    floor is suppressed from live email and only ever shows up in this
    subscription's next digest -- see
    tenant_digest_service.is_live_suppressed. Doesn't touch in-app
    Notification Center delivery or Slack/Teams/webhook fan-out, both of
    which stay real-time regardless -- this only gates the *email*
    channel, which is the one a digest is meant to replace for
    low-severity noise.
    """

    ALL = "all"  # nothing digest-only -- every severity still pages live
    WARNING = "warning"  # only "warning" and below roll into the digest
    CRITICAL = "critical"  # only "critical" and below roll into the digest (i.e. nothing pages live)


class TenantDigestSubscription(Base):
    """One tenant's standing subscription to a periodic Alert/Incident/
    AuditLog activity rollup, emailed by
    app.services.tenant_digest_service.deliver_due_subscription.
    """

    __tablename__ = "tenant_digest_subscriptions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)

    cadence = Column(Enum(DigestCadence), nullable=False, default=DigestCadence.WEEKLY)
    # Hour of day (0-23, UTC) the digest goes out.
    hour_utc = Column(Integer, nullable=False, default=8)
    # Day of week for WEEKLY cadence only (0=Monday..6=Sunday, matches
    # datetime.weekday()); ignored for DAILY. Nullable rather than
    # defaulted to 0 so the dispatcher can tell "not applicable" apart
    # from "explicitly Monday" if that distinction ever matters.
    day_of_week = Column(Integer, nullable=True)

    # Comma-separated, same convention as
    # NotificationSettings.recipients -- this subscription's own
    # delivery list rather than the operator-wide NOTIFY_EMAIL_RECIPIENTS/
    # NotificationSettings.recipients, since a tenant's digest usually
    # goes to that tenant's contacts, not the MSP's global list.
    recipients = Column(String, nullable=False)

    # See DigestSeverityFloor -- gates the live email channel only.
    severity_floor = Column(Enum(DigestSeverityFloor), nullable=False, default=DigestSeverityFloor.ALL)

    is_active = Column(Boolean, nullable=False, default=True, server_default="true")

    # Advances after every delivery attempt (sent or skipped-empty) so
    # the next digest's window is "since last send" rather than a fixed
    # trailing window -- see tenant_digest_service.build_digest. Starts
    # NULL (created_at is used as the first window's start).
    last_sent_at = Column(DateTime(timezone=True), nullable=True)

    created_by = Column(String, nullable=True)  # user email
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    tenant = relationship("Tenant")

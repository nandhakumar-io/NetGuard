import enum
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.database import Base


class PushProvider(str, enum.Enum):
    NTFY = "ntfy"
    PUSHOVER = "pushover"
    # Native browser push (Web Push API / VAPID) -- no separate mobile app
    # needed, works via the service worker registered at /sw.js. See
    # app.services.push_service._send_browser. `target` for this provider
    # is a JSON blob (endpoint/p256dh/auth), not a bare URL or key, unlike
    # the other two providers.
    BROWSER = "browser"


class PushSubscription(Base):
    """A single mobile device registered to receive real phone push
    notifications (via ntfy or Pushover) for critical incidents and
    alert escalations -- see app.services.push_service. Self-scoped:
    every row belongs to exactly one user (app.api.push_subscriptions),
    same as notification preferences.
    """

    __tablename__ = "push_subscriptions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    label = Column(String, nullable=False, default="My Phone")
    provider = Column(Enum(PushProvider, name="pushprovider", values_callable=lambda obj: [e.value for e in obj]), nullable=False, default=PushProvider.NTFY)
    # ntfy topic URL, or Pushover user key. Not encrypted at rest -- an
    # ntfy topic URL is only as secret as a bookmarked link (ntfy's own
    # security model), and a Pushover user key alone can't send
    # anything without our app token, so this doesn't carry the same
    # exposure as a credential (see app.services.credential_service for
    # what *does* get encrypted).
    target = Column(String, nullable=False)
    include_non_critical = Column(Boolean, nullable=False, default=False)
    # JSON-encoded list of response actions to attach to the push itself,
    # e.g. ["acknowledge","escalate","run_runbook"]. Rendered as native
    # ntfy action buttons (deep links back into NetGuard) on the ntfy
    # provider; stored but not yet actionable on Pushover/browser, whose
    # notification-action surfaces are more limited. NULL/empty = plain
    # notification. See app.services.push_service._send_ntfy.
    include_actions = Column(String, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_pushed_at = Column(DateTime(timezone=True), nullable=True)

    delivery_attempts = relationship(
        "PushDeliveryAttempt", back_populates="push_subscription",
        cascade="all, delete-orphan", order_by="PushDeliveryAttempt.attempted_at.desc()",
    )


class PushDeliveryAttempt(Base):
    """One outbound push notification_service/push_service attempted (or
    skipped) for a PushSubscription -- success, provider failure, or
    filtered out before ever hitting the network. Same rationale as
    app.models.webhook.WebhookDeliveryAttempt: without a durable log, "my
    phone never buzzed" is indistinguishable from "it was never sent",
    "the provider rejected it", or "it was filtered by
    include_non_critical" short of grepping server logs -- and severity
    filtering in particular (see push_service.send_push) is a common,
    easy-to-miss reason a subscription looks broken when it's actually
    working exactly as configured.
    """

    __tablename__ = "push_delivery_attempts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    push_subscription_id = Column(
        UUID(as_uuid=True), ForeignKey("push_subscriptions.id"), nullable=False, index=True
    )

    event = Column(String, nullable=False)  # human-readable event title, e.g. "Interface Down"
    severity = Column(String, nullable=True)  # "info" | "warning" | "critical" | "resolved"
    provider = Column(String, nullable=True)  # snapshot of the subscription's provider at send time

    # True only for an attempt that actually reached the provider's API
    # (ntfy/Pushover HTTP call, or Web Push) and got a non-error
    # response. A filtered/skipped attempt is always success=False with
    # skipped=True instead, so the two "didn't get a push" causes stay
    # distinguishable in the log.
    success = Column(Boolean, nullable=False, default=False, server_default="false")

    # True when this attempt never reached the provider at all because
    # send_push's own filtering held it back (severity below the
    # subscription's include_non_critical threshold, or the subscription
    # was disabled) -- as opposed to a real delivery failure the
    # provider rejected. `skip_reason` is a short machine string, e.g.
    # "severity_below_threshold".
    skipped = Column(Boolean, nullable=False, default=False, server_default="false")
    skip_reason = Column(String, nullable=True)

    error = Column(Text, nullable=True)  # exception text when the provider call itself failed

    attempted_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    push_subscription = relationship("PushSubscription", back_populates="delivery_attempts")

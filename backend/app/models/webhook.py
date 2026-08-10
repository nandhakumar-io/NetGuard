"""WebhookEndpoint — user-configurable notification delivery targets.

Each row represents one outbound webhook that notification_service fans
out to after every `notify()` call.  Supports generic HTTP POST, Slack,
Teams, and Telegram webhook types, each with a slightly different payload
format.
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
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.database import Base


class WebhookType(str, enum.Enum):
    GENERIC = "generic"
    SLACK = "slack"
    TEAMS = "teams"
    TELEGRAM = "telegram"


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    url = Column(String, nullable=False)
    webhook_type = Column(Enum(WebhookType), nullable=False, default=WebhookType.GENERIC)

    # Optional shared secret for HMAC signature verification on the
    # receiving end (X-Webhook-Signature header). NULL = no signing.
    secret = Column(String, nullable=True)

    # JSON-encoded list of event types to subscribe to, e.g.
    # ["deployment_failed","drift_critical"]. NULL or empty = all events.
    events = Column(Text, nullable=True)

    # For Telegram type: the chat_id to send messages to
    # (url should be the bot token URL in that case).
    telegram_chat_id = Column(String, nullable=True)

    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    created_by = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    delivery_attempts = relationship(
        "WebhookDeliveryAttempt", back_populates="webhook_endpoint",
        cascade="all, delete-orphan", order_by="WebhookDeliveryAttempt.attempted_at.desc()",
    )


class WebhookDeliveryAttempt(Base):
    """One outbound HTTP call notification_service made (or a manual
    retry of one) to a WebhookEndpoint -- success or failure. Backs the
    delivery log / retry UI on the Alert Center Webhooks tab: without
    this table, a failed delivery just vanished into the backend log
    (see notification_service._fan_out_user_webhooks before this was
    added) and there was no way to tell "the webhook is misconfigured"
    from "it's never fired at all" short of grepping server logs, and no
    way to resend a notification that failed transiently (endpoint was
    briefly down, network blip) without waiting for the next real event.
    """

    __tablename__ = "webhook_delivery_attempts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    webhook_endpoint_id = Column(
        UUID(as_uuid=True), ForeignKey("webhook_endpoints.id"), nullable=False, index=True
    )

    event = Column(String, nullable=False)  # human-readable event title, e.g. "Deployment Failed"
    event_type = Column(String, nullable=True)  # machine event_type used for the subscription filter
    severity = Column(String, nullable=True)  # "info" | "warning" | "critical"

    # The exact JSON body sent on the wire (post-formatting for the
    # endpoint's webhook_type, e.g. the Slack {"text": ...} envelope, not
    # the generic NetGuard payload shape) -- stored so a retry replays
    # exactly what was attempted, and so the UI can show "what did we
    # actually send" without reconstructing it from the other columns.
    request_payload = Column(Text, nullable=True)

    success = Column(Boolean, nullable=False, default=False, server_default="false")
    status_code = Column(Integer, nullable=True)  # NULL when the request never got an HTTP response
    response_body = Column(Text, nullable=True)  # truncated to a few hundred chars, see notification_service
    error = Column(Text, nullable=True)  # exception text when the request itself failed (timeout, DNS, ...)

    # Manual-retry lineage: True + retry_of_id set for an attempt created
    # via POST /webhooks/deliveries/{id}/retry, rather than a normal
    # event-triggered delivery. Self-referential rather than a boolean
    # flag alone so the UI/audit trail can trace "this succeeded, but
    # only on the 2nd try" back to the original failed attempt.
    is_retry = Column(Boolean, nullable=False, default=False, server_default="false")
    retry_of_id = Column(UUID(as_uuid=True), ForeignKey("webhook_delivery_attempts.id"), nullable=True)
    retried_by = Column(String, nullable=True)  # user email who triggered the manual retry, if is_retry

    attempted_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    webhook_endpoint = relationship("WebhookEndpoint", back_populates="delivery_attempts")
    retry_of = relationship("WebhookDeliveryAttempt", remote_side=[id])

"""WebhookEndpoint — user-configurable notification delivery targets.

Each row represents one outbound webhook that notification_service fans
out to after every `notify()` call.  Supports generic HTTP POST, Slack,
Teams, and Telegram webhook types, each with a slightly different payload
format.
"""
import enum
import uuid

from sqlalchemy import Boolean, Column, DateTime, Enum, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

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

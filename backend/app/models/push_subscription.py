import enum
import uuid

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID

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
    provider = Column(Enum(PushProvider, name="pushprovider"), nullable=False, default=PushProvider.NTFY)
    # ntfy topic URL, or Pushover user key. Not encrypted at rest -- an
    # ntfy topic URL is only as secret as a bookmarked link (ntfy's own
    # security model), and a Pushover user key alone can't send
    # anything without our app token, so this doesn't carry the same
    # exposure as a credential (see app.services.credential_service for
    # what *does* get encrypted).
    target = Column(String, nullable=False)
    include_non_critical = Column(Boolean, nullable=False, default=False)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_pushed_at = Column(DateTime(timezone=True), nullable=True)

import datetime
import uuid

from pydantic import BaseModel, ConfigDict


class PushSubscriptionCreate(BaseModel):
    label: str = "My Phone"
    provider: str = "ntfy"  # ntfy | pushover | browser
    # ntfy topic URL, or Pushover user key. Not used for provider="browser"
    # -- that shape comes from the browser's own PushSubscription object
    # via endpoint/p256dh/auth below, not something a user types in.
    target: str | None = None
    include_non_critical: bool = False
    # Subset of ["acknowledge", "escalate", "run_runbook"] -- which
    # one-tap action buttons to attach to the push itself.
    include_actions: list[str] | None = None
    # Only used when provider="browser": the three fields off the
    # browser's PushSubscription.toJSON() (endpoint, and keys.p256dh /
    # keys.auth) captured right after pushManager.subscribe() succeeds.
    endpoint: str | None = None
    p256dh: str | None = None
    auth: str | None = None


class PushSubscriptionUpdate(BaseModel):
    label: str | None = None
    target: str | None = None
    include_non_critical: bool | None = None
    include_actions: list[str] | None = None
    enabled: bool | None = None


class PushSubscriptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label: str
    provider: str
    target: str
    include_non_critical: bool
    include_actions: list[str] | None = None
    enabled: bool
    created_at: datetime.datetime | None = None
    last_pushed_at: datetime.datetime | None = None


class PushTestResult(BaseModel):
    sent: bool
    message: str


class VapidPublicKeyResponse(BaseModel):
    configured: bool
    public_key: str | None = None

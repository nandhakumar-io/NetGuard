import datetime
import uuid

from pydantic import BaseModel, ConfigDict


class PushSubscriptionCreate(BaseModel):
    label: str = "My Phone"
    provider: str = "ntfy"  # ntfy | pushover
    target: str  # ntfy topic URL, or Pushover user key
    include_non_critical: bool = False


class PushSubscriptionUpdate(BaseModel):
    label: str | None = None
    target: str | None = None
    include_non_critical: bool | None = None
    enabled: bool | None = None


class PushSubscriptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label: str
    provider: str
    target: str
    include_non_critical: bool
    enabled: bool
    created_at: datetime.datetime | None = None
    last_pushed_at: datetime.datetime | None = None


class PushTestResult(BaseModel):
    sent: bool
    message: str

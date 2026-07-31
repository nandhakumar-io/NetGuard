"""Pydantic schemas for the in-app Notification Center API (SRS FR-11)."""
import uuid
from datetime import datetime

from pydantic import BaseModel


class NotificationRead(BaseModel):
    id: uuid.UUID
    event_type: str
    severity: str
    title: str
    message: str
    device_hostname: str | None = None
    change_request_id: uuid.UUID | None = None
    deployment_id: uuid.UUID | None = None
    read: bool
    created_at: datetime

    class Config:
        from_attributes = True


class NotificationMarkRead(BaseModel):
    """Body is empty -- the notification id comes from the URL."""
    pass


class NotificationSummary(BaseModel):
    unread_count: int
    total: int
"""Pydantic schemas for the Alert API."""
import uuid
from datetime import datetime

from pydantic import BaseModel


class AlertRead(BaseModel):
    id: uuid.UUID
    device_id: uuid.UUID | None = None
    severity: str
    source: str
    category: str
    message: str
    acknowledged: bool
    acknowledged_by: str | None = None
    resolved: bool
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class AlertAcknowledge(BaseModel):
    """Body is empty — the current user is inferred from the JWT."""
    pass


class AlertResolve(BaseModel):
    """Body is empty — the current user is inferred from the JWT."""
    pass


class AlertSummary(BaseModel):
    critical: int
    warning: int
    info: int
    active_total: int
    resolved: int

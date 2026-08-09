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
    last_seen_at: datetime | None = None
    occurrence_count: int = 1
    root_cause_alert_id: uuid.UUID | None = None
    suppressed: bool = False
    suppressed_by_window_id: uuid.UUID | None = None
    # Populated in app.api.alerts from the joined AlertSnooze -- present
    # (and in the future) only while a matching snooze is still active;
    # an alert whose snooze has expired reports None here even though
    # muted_by_snooze_id is still set on the row (history, not a live
    # mute -- see app.services.alert_snooze_service.active_mute_map).
    muted_until: datetime | None = None
    escalated: bool = False
    escalated_at: datetime | None = None
    last_escalated_at: datetime | None = None
    escalation_count: int = 0
    escalation_policy_id: uuid.UUID | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class AlertAcknowledge(BaseModel):
    """Body is empty — the current user is inferred from the JWT."""


class AlertResolve(BaseModel):
    """Body is empty — the current user is inferred from the JWT."""


class AlertSummary(BaseModel):
    critical: int
    warning: int
    info: int
    active_total: int
    resolved: int

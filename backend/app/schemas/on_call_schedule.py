"""Pydantic schemas for the On-Call Schedule CRUD API. See
app.models.on_call_schedule and app.services.on_call_service.
"""
import uuid
from datetime import datetime

from pydantic import BaseModel


class OnCallScheduleCreate(BaseModel):
    name: str
    description: str | None = None
    primary_user_email: str
    secondary_user_email: str | None = None
    rotation_type: str = "none"  # none / daily / weekly
    shift_handover_time: str | None = None  # "HH:MM", 24h
    timezone: str | None = None  # IANA zone, e.g. "America/New_York"
    enabled: bool = True


class OnCallScheduleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    primary_user_email: str | None = None
    secondary_user_email: str | None = None
    rotation_type: str | None = None
    shift_handover_time: str | None = None
    timezone: str | None = None
    enabled: bool | None = None


class OnCallScheduleRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    primary_user_email: str
    secondary_user_email: str | None = None
    rotation_type: str
    shift_handover_time: str | None = None
    timezone: str | None = None
    enabled: bool
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class OnCallScheduleCurrent(BaseModel):
    """Who's on call right now for a given schedule -- powers the small
    "currently on call: ..." hint on the Escalation Policies page next
    to the schedule picker."""

    schedule_id: uuid.UUID
    current_contact: str | None = None
    is_secondary: bool = False

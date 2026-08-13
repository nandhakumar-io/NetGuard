"""Pydantic schemas for the AlertRunbook CRUD API."""
import uuid
from datetime import datetime

from pydantic import BaseModel


class AlertRunbookCreate(BaseModel):
    category: str
    source: str | None = None  # snmp_trap / health_poll / drift / protocol_failure / syslog / None=any
    title: str
    url: str
    notes: str | None = None


class AlertRunbookUpdate(BaseModel):
    category: str | None = None
    source: str | None = None
    title: str | None = None
    url: str | None = None
    notes: str | None = None


class AlertRunbookRead(BaseModel):
    id: uuid.UUID
    category: str
    source: str | None = None
    title: str
    url: str
    notes: str | None = None
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class RunbookRef(BaseModel):
    """Lightweight runbook reference embedded on an AlertRead."""

    title: str
    url: str

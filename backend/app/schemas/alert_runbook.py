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
    remediation_enabled: bool = False
    remediation_action_type: str | None = None  # restart_service / push_config
    remediation_label: str | None = None
    remediation_command: str | None = None
    remediation_required_role: str | None = None


class AlertRunbookUpdate(BaseModel):
    category: str | None = None
    source: str | None = None
    title: str | None = None
    url: str | None = None
    notes: str | None = None
    remediation_enabled: bool | None = None
    remediation_action_type: str | None = None
    remediation_label: str | None = None
    remediation_command: str | None = None
    remediation_required_role: str | None = None


class AlertRunbookRead(BaseModel):
    id: uuid.UUID
    category: str
    source: str | None = None
    title: str
    url: str
    notes: str | None = None
    remediation_enabled: bool = False
    remediation_action_type: str | None = None
    remediation_label: str | None = None
    remediation_command: str | None = None
    remediation_required_role: str | None = None
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class RunbookRef(BaseModel):
    """Lightweight runbook reference embedded on an AlertRead."""

    id: uuid.UUID
    title: str
    url: str
    remediation_enabled: bool = False


class RunbookExecutionRequest(BaseModel):
    device_id: uuid.UUID
    alert_id: uuid.UUID | None = None


class RunbookExecutionRead(BaseModel):
    id: uuid.UUID
    runbook_id: uuid.UUID
    alert_id: uuid.UUID | None = None
    device_id: uuid.UUID
    triggered_by: str
    status: str
    output: str | None = None
    error: str | None = None
    created_at: datetime
    completed_at: datetime | None = None

    class Config:
        from_attributes = True

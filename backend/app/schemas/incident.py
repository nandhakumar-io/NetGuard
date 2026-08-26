"""Pydantic schemas for the Incident / postmortem API."""
import uuid
from datetime import datetime

from pydantic import BaseModel

from app.schemas.alert_runbook import RunbookRef


class IncidentCreate(BaseModel):
    """Open an incident from a correlated alert group. `root_cause_alert_id`
    is the anchor alert (see alert_correlation_service) -- its currently
    -suppressed dependents are folded in automatically; pass explicit
    `alert_ids` instead if you're building the incident by hand."""

    title: str
    summary: str | None = None
    severity: str = "major"  # critical / major / minor
    root_cause_alert_id: uuid.UUID | None = None
    alert_ids: list[uuid.UUID] | None = None
    detected_at: datetime | None = None


class IncidentUpdate(BaseModel):
    title: str | None = None
    summary: str | None = None
    severity: str | None = None
    status: str | None = None
    mitigated_at: datetime | None = None
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    root_cause_summary: str | None = None
    impact_summary: str | None = None
    action_items: str | None = None


class TimelineEventCreate(BaseModel):
    event_type: str = "note"  # detection | mitigation | resolution | note | status_change
    description: str
    occurred_at: datetime | None = None


class TimelineEventRead(BaseModel):
    id: uuid.UUID
    event_type: str
    description: str
    actor: str | None = None
    occurred_at: datetime

    class Config:
        from_attributes = True


class IncidentRead(BaseModel):
    id: uuid.UUID
    title: str
    summary: str | None = None
    severity: str
    status: str
    root_cause_alert_id: uuid.UUID | None = None
    alert_ids: list[uuid.UUID] = []
    detected_at: datetime | None = None
    mitigated_at: datetime | None = None
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    root_cause_summary: str | None = None
    impact_summary: str | None = None
    action_items: str | None = None
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    # Resolved in app.api.incidents from the root-cause alert's
    # category(+source), same lookup AlertRead.runbook uses -- lets the
    # incident view surface the same remediation doc that would've shown
    # on the alert itself, without needing category/source stored again
    # on Incident.
    runbook: RunbookRef | None = None
    tenant_name: str | None = None


class IncidentDetailRead(IncidentRead):
    timeline: list[TimelineEventRead] = []

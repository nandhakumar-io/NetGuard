"""Pydantic schemas for the Escalation Policy CRUD API + escalation log."""
import uuid
from datetime import datetime

from pydantic import BaseModel


class EscalationPolicyCreate(BaseModel):
    name: str
    description: str | None = None
    severity_scope: str = "critical"  # critical / warning / all
    unack_minutes: int = 15
    repeat_minutes: int | None = None
    secondary_contacts: str | None = None  # comma-separated emails
    on_call_schedule_id: uuid.UUID | None = None
    channel: str = "email"  # email / webhook / slack / teams / push
    webhook_url: str | None = None
    enabled: bool = True
    # Set to override a global (MSP-authored) policy instead of adding an
    # unrelated one alongside it -- see EscalationPolicy.parent_policy_id.
    # Only meaningful for a tenant-owned policy; ignored for a policy
    # created by MSP staff (tenant_id will be NULL regardless).
    parent_policy_id: uuid.UUID | None = None


class EscalationPolicyUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    severity_scope: str | None = None
    unack_minutes: int | None = None
    repeat_minutes: int | None = None
    secondary_contacts: str | None = None
    on_call_schedule_id: uuid.UUID | None = None
    channel: str | None = None
    webhook_url: str | None = None
    enabled: bool | None = None
    parent_policy_id: uuid.UUID | None = None


class EscalationPolicyRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    severity_scope: str
    unack_minutes: int
    repeat_minutes: int | None = None
    secondary_contacts: str | None = None
    on_call_schedule_id: uuid.UUID | None = None
    channel: str
    webhook_url: str | None = None
    enabled: bool
    created_by: str | None = None
    # NULL = global/MSP-authored, applies across every tenant.
    tenant_id: uuid.UUID | None = None
    parent_policy_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class EscalatedAlertRead(BaseModel):
    """One entry in the escalation log/feed -- an alert that has been
    escalated at least once, with the policy that fired and how many
    times it's repeated."""

    id: uuid.UUID
    device_id: uuid.UUID | None = None
    severity: str
    category: str
    message: str
    acknowledged: bool
    escalated_at: datetime | None = None
    last_escalated_at: datetime | None = None
    escalation_count: int
    escalation_policy_id: uuid.UUID | None = None
    escalation_policy_name: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True

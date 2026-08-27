"""Pydantic schemas for the Tenant Digest Subscription CRUD API."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TenantDigestSubscriptionCreate(BaseModel):
    tenant_id: uuid.UUID
    cadence: str = "weekly"  # daily / weekly
    hour_utc: int = 8
    day_of_week: int | None = None  # 0=Monday..6=Sunday, required for weekly
    recipients: str  # comma-separated emails
    severity_floor: str = "all"  # all / warning / critical
    is_active: bool = True


class TenantDigestSubscriptionUpdate(BaseModel):
    cadence: str | None = None
    hour_utc: int | None = None
    day_of_week: int | None = None
    recipients: str | None = None
    severity_floor: str | None = None
    is_active: bool | None = None


class TenantDigestSubscriptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    cadence: str
    hour_utc: int
    day_of_week: int | None = None
    recipients: str
    severity_floor: str
    is_active: bool
    last_sent_at: datetime | None = None
    created_by: str | None = None
    created_at: datetime

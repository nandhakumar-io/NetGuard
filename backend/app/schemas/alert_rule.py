"""Pydantic schemas for the AlertRule CRUD API."""
import uuid
from datetime import datetime

from pydantic import BaseModel


class AlertRuleCreate(BaseModel):
    name: str
    description: str | None = None
    metric: str  # cpu / memory / bandwidth / temperature / uptime /
    # interface_errors / interface_down_count / fan_failure / power_supply_failure /
    # trunk_port_down / sfp_port_down / route_unreachable / ping_packet_loss_pct
    operator: str  # gt / gte / lt / lte / eq
    threshold: float
    severity: str = "warning"
    scope_vendor: str | None = None
    scope_site: str | None = None
    scope_device_role: str | None = None
    cooldown_seconds: int = 300
    enabled: bool = True


class AlertRuleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    metric: str | None = None  # see AlertRuleCreate.metric for the full list
    operator: str | None = None
    threshold: float | None = None
    severity: str | None = None
    scope_vendor: str | None = None
    scope_site: str | None = None
    scope_device_role: str | None = None
    cooldown_seconds: int | None = None
    enabled: bool | None = None


class AlertRuleRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    metric: str
    operator: str
    threshold: float
    severity: str
    scope_vendor: str | None = None
    scope_site: str | None = None
    scope_device_role: str | None = None
    cooldown_seconds: int
    enabled: bool
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True

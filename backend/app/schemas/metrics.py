import datetime
import uuid

from pydantic import BaseModel, ConfigDict

from app.models.device_metric import HealthColor


class DeviceMetricRead(BaseModel):
    """One SNMP health-poll snapshot -- the unit both the latest-reading
    endpoint and the historical-chart endpoint return."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    device_id: uuid.UUID
    cpu_utilization_pct: float | None = None
    memory_utilization_pct: float | None = None
    interface_utilization_pct: float | None = None
    interface_errors: int | None = None
    temperature_celsius: float | None = None
    fan_status: str | None = None
    power_supply_status: str | None = None
    uptime_seconds: int | None = None
    health_score: int | None = None
    health_color: HealthColor | None = None
    polled_at: datetime.datetime


class DeviceHealthSummary(BaseModel):
    """Health Dashboard card for a single device."""

    device_id: uuid.UUID
    hostname: str
    health_score: int | None = None
    health_color: str = "unknown"
    reachable: bool = False
    latest_metric: DeviceMetricRead | None = None


class FleetHealthSummary(BaseModel):
    """Top-of-dashboard fleet rollup: how many SNMP-monitored devices are
    green/yellow/red right now."""

    devices_monitored: int
    green: int
    yellow: int
    red: int
    unknown: int
    average_health_score: int | None = None


class AlertRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    device_id: uuid.UUID | None = None
    severity: str
    source: str
    category: str
    message: str
    acknowledged: bool
    created_at: datetime.datetime
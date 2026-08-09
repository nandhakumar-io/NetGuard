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


class MetricFreshness(BaseModel):
    """Per-metric last-successful-read timestamps (ISO strings, None if
    that metric has never once resolved for this device) -- lets a card
    with a healthy overall score still flag e.g. a stale interface table
    instead of reading as fully green."""

    cpu: str | None = None
    memory: str | None = None
    interface: str | None = None
    temperature: str | None = None
    fan: str | None = None
    power: str | None = None


class DeviceHealthSummary(BaseModel):
    """Health Dashboard card for a single device."""

    device_id: uuid.UUID
    hostname: str
    health_score: int | None = None
    health_color: str = "unknown"
    reachable: bool = False
    latest_metric: DeviceMetricRead | None = None
    metric_freshness: MetricFreshness | None = None
    # Names (matching MetricFreshness's fields, e.g. "interface") of any
    # metric families that used to resolve but have fallen behind this
    # device's own recent polls -- lets a card that's green overall still
    # flag e.g. a stale interface table instead of reading as fully
    # healthy. See metrics_service.stale_metric_names.
    stale_metrics: list[str] = []


class FleetHealthSummary(BaseModel):
    """Top-of-dashboard fleet rollup: how many SNMP-monitored devices are
    green/yellow/red right now."""

    devices_monitored: int
    green: int
    yellow: int
    red: int
    unknown: int
    average_health_score: int | None = None
    # Count of devices with at least one stale metric family (see
    # DeviceHealthSummary.stale_metrics) -- a fleet that's green-on-paper
    # can still be hiding a data-completeness gap.
    devices_with_stale_metrics: int = 0


class DeviceAvailability(BaseModel):
    """One device's time-weighted availability % within the rollup
    window -- see FleetAvailabilitySummary.worst_devices."""

    device_id: uuid.UUID
    hostname: str
    availability_pct: float


class FleetAvailabilitySummary(BaseModel):
    """NOC-style "how many nines" fleet uptime rollup, time-weighted from
    DeviceStatusHistory over the requested window (see
    app.services.metrics_service.fleet_availability_summary)."""

    window_hours: int
    devices_in_rollup: int
    fleet_availability_pct: float | None = None
    fleet_availability_label: str  # e.g. "99.95%", or "n/a" if nothing to roll up yet
    worst_devices: list[DeviceAvailability] = []


class UnstableDevice(BaseModel):
    """One device's combined instability signal -- reachability flaps +
    interface flaps + config drift events, all within the same window --
    see app.services.metrics_service.unstable_devices."""

    device_id: uuid.UUID
    hostname: str
    reachability_flaps: int
    interface_flaps: int
    drift_events: int
    instability_score: int


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

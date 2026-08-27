"""Alert rule dry-run / backtest -- "would this rule have fired against
real metric history?" preview, so a threshold can be sanity-checked
against the last N days of actual fleet data before it's saved and left
to page someone at 3am.

Runs the *same* breach/cooldown state machine as
app.services.alert_rule_engine.evaluate_rules, just replayed over
historical device_metric_history() rows (VictoriaMetrics, see
app.core.vm_client) instead of one live SnmpMetrics sample per device
per poll -- so "what would have fired" and "what actually fires" can
never quietly disagree on the logic, only on the data source.

Only metrics VictoriaMetrics actually has a time series for can be
backtested -- see SUPPORTED_BACKTEST_METRICS. The live-poll-only metrics
(trunk_port_down, sfp_port_down, route_unreachable,
ping_packet_loss_pct, interface_down_count, fan_failure,
power_supply_failure) are computed from a single live poll's structures
(per-interface list, routing table walk, ping burst, status strings)
that are never persisted as their own time series, so there's nothing
to replay -- returning a fabricated "0 firings" for those would be
actively misleading (indistinguishable from "we checked and it never
breached"), so a rule using one of them reports unsupported instead,
matching the "don't invent data we don't have" posture already used at
read time in alert_rule_engine._metric_value.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core import vm_client
from app.core.config import settings
from app.models.device import Device

_OPERATORS = {
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "eq": lambda a, b: a == b,
}

# metric -> the device_metric_history() field it reads. Deliberately a
# subset of alert_rule_engine._metric_value's full metric list -- see
# module docstring for why the rest can't be backtested.
SUPPORTED_BACKTEST_METRICS = {
    "cpu": "cpu_utilization_pct",
    "memory": "memory_utilization_pct",
    "bandwidth": "interface_utilization_pct",
    "temperature": "temperature_celsius",
    "uptime": "uptime_seconds",
    "interface_errors": "interface_errors",
}

UNSUPPORTED_METRIC_REASON = (
    "'{metric}' is computed from a single live poll (not stored as its own "
    "time series), so there's no history to replay it against. Supported "
    "metrics for dry-run: " + ", ".join(sorted(SUPPORTED_BACKTEST_METRICS))
)

# Hard ceiling regardless of what the caller asks for -- VictoriaMetrics
# only retains SNMP_METRIC_RETENTION_DAYS worth of samples (see
# core/vm_client's write path / SNMP_METRIC_RETENTION_DAYS), so asking
# for more than that just silently returns a shorter window; capping the
# request up front keeps the response honest about what it actually
# covers.
MAX_LOOKBACK_HOURS = settings.SNMP_METRIC_RETENTION_DAYS * 24

# Firing intervals returned in the response, most-recent-first -- a rule
# that's genuinely this chatty over the window has already made its
# point well before 200 rows, and an unbounded list on a truly flappy
# metric could be tens of thousands of rows.
MAX_FIRINGS_RETURNED = 200

# Range-query resolution. Coarser than the live poll interval on purpose
# -- a dry-run is estimating firing *volume*, not reconstructing
# millisecond-accurate transition timestamps, and one query per device
# already fans out to several PromQL calls (see vm_client.
# device_metric_history), so keeping the step reasonably coarse matters
# more here than for a single-device chart.
_STEP_SECONDS = 300


@dataclass
class Firing:
    device_id: str
    hostname: str
    fired_at: datetime
    cleared_at: datetime | None
    peak_value: float

    @property
    def duration_seconds(self) -> float | None:
        if self.cleared_at is None:
            return None
        return (self.cleared_at - self.fired_at).total_seconds()


@dataclass
class DeviceSummary:
    device_id: str
    hostname: str
    firing_count: int
    suppressed_by_cooldown_count: int
    total_seconds_breached: float
    samples_evaluated: int


@dataclass
class BacktestResult:
    supported: bool
    unsupported_reason: str | None
    metric: str
    operator: str
    threshold: float
    cooldown_seconds: int
    window_start: datetime
    window_end: datetime
    devices_matched: int
    devices_with_data: int
    total_firings: int
    total_suppressed_by_cooldown: int
    estimated_alerts_per_day: float
    firings: list[Firing] = field(default_factory=list)
    per_device: list[DeviceSummary] = field(default_factory=list)


def _scope_matches(device: Device, scope_vendor: str | None, scope_site: str | None, scope_device_role: str | None) -> bool:
    if scope_vendor and (device.vendor is None or scope_vendor.lower() != str(device.vendor.value).lower()):
        return False
    if scope_site and (device.site is None or scope_site.lower() != device.site.lower()):
        return False
    if scope_device_role and (
        device.device_role is None or scope_device_role.lower() != device.device_role.lower()
    ):
        return False
    return True


def _matching_devices(
    db: Session, tenant_id, scope_vendor: str | None, scope_site: str | None, scope_device_role: str | None
) -> list[Device]:
    q = db.query(Device)
    if tenant_id is not None:
        q = q.filter(Device.tenant_id == tenant_id)
    return [d for d in q.all() if _scope_matches(d, scope_vendor, scope_site, scope_device_role)]


def _replay_device(
    device: Device, rows: list[dict], field_name: str, op_fn, threshold: float, cooldown_seconds: int
) -> tuple[list[Firing], int, float]:
    """Runs the same breach/cooldown state machine as
    alert_rule_engine.evaluate_rules over one device's historical rows
    (already sorted ascending by polled_at). Returns (firings,
    suppressed_by_cooldown_count, total_seconds_breached)."""
    firings: list[Firing] = []
    suppressed = 0
    total_breached_seconds = 0.0

    current: Firing | None = None
    last_cleared_at: datetime | None = None
    last_row_at: datetime | None = None

    for row in rows:
        value = row.get(field_name)
        polled_at = row["polled_at"]
        if value is None:
            continue
        breached = op_fn(value, threshold)

        if current is not None and last_row_at is not None:
            total_breached_seconds += (polled_at - last_row_at).total_seconds()

        if breached and current is None:
            if last_cleared_at is not None and (polled_at - last_cleared_at).total_seconds() < cooldown_seconds:
                suppressed += 1
            else:
                current = Firing(
                    device_id=str(device.id), hostname=device.hostname,
                    fired_at=polled_at, cleared_at=None, peak_value=value,
                )
        elif breached and current is not None:
            current.peak_value = max(current.peak_value, value)
        elif not breached and current is not None:
            current.cleared_at = polled_at
            firings.append(current)
            last_cleared_at = polled_at
            current = None

        last_row_at = polled_at

    if current is not None:
        firings.append(current)  # still breached at window end -- cleared_at stays None

    return firings, suppressed, total_breached_seconds


def backtest_rule(
    db: Session,
    *,
    tenant_id,
    metric: str,
    operator: str,
    threshold: float,
    cooldown_seconds: int = 300,
    scope_vendor: str | None = None,
    scope_site: str | None = None,
    scope_device_role: str | None = None,
    lookback_hours: int = 168,
) -> BacktestResult:
    metric = metric.lower()
    operator = operator.lower()
    lookback_hours = max(1, min(lookback_hours, MAX_LOOKBACK_HOURS))
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=lookback_hours)

    field_name = SUPPORTED_BACKTEST_METRICS.get(metric)
    if field_name is None:
        return BacktestResult(
            supported=False,
            unsupported_reason=UNSUPPORTED_METRIC_REASON.format(metric=metric),
            metric=metric, operator=operator, threshold=threshold, cooldown_seconds=cooldown_seconds,
            window_start=window_start, window_end=now,
            devices_matched=0, devices_with_data=0, total_firings=0, total_suppressed_by_cooldown=0,
            estimated_alerts_per_day=0.0,
        )

    op_fn = _OPERATORS.get(operator)
    if op_fn is None:
        return BacktestResult(
            supported=False,
            unsupported_reason=f"'{operator}' is not a recognized operator (gt/gte/lt/lte/eq).",
            metric=metric, operator=operator, threshold=threshold, cooldown_seconds=cooldown_seconds,
            window_start=window_start, window_end=now,
            devices_matched=0, devices_with_data=0, total_firings=0, total_suppressed_by_cooldown=0,
            estimated_alerts_per_day=0.0,
        )

    devices = _matching_devices(db, tenant_id, scope_vendor, scope_site, scope_device_role)

    all_firings: list[Firing] = []
    per_device: list[DeviceSummary] = []
    devices_with_data = 0
    total_suppressed = 0

    for device in devices:
        rows = vm_client.device_metric_history(device.id, window_start, now, step_seconds=_STEP_SECONDS)
        if not rows:
            continue
        devices_with_data += 1
        rows.sort(key=lambda r: r["polled_at"])

        firings, suppressed, breached_seconds = _replay_device(device, rows, field_name, op_fn, threshold, cooldown_seconds)
        all_firings.extend(firings)
        total_suppressed += suppressed
        if firings or suppressed:
            per_device.append(
                DeviceSummary(
                    device_id=str(device.id), hostname=device.hostname,
                    firing_count=len(firings), suppressed_by_cooldown_count=suppressed,
                    total_seconds_breached=breached_seconds, samples_evaluated=len(rows),
                )
            )

    all_firings.sort(key=lambda f: f.fired_at, reverse=True)
    per_device.sort(key=lambda d: d.firing_count, reverse=True)

    days = max(lookback_hours / 24.0, 1e-9)
    estimated_alerts_per_day = round(len(all_firings) / days, 2)

    return BacktestResult(
        supported=True,
        unsupported_reason=None,
        metric=metric, operator=operator, threshold=threshold, cooldown_seconds=cooldown_seconds,
        window_start=window_start, window_end=now,
        devices_matched=len(devices), devices_with_data=devices_with_data,
        total_firings=len(all_firings), total_suppressed_by_cooldown=total_suppressed,
        estimated_alerts_per_day=estimated_alerts_per_day,
        firings=all_firings[:MAX_FIRINGS_RETURNED],
        per_device=per_device,
    )

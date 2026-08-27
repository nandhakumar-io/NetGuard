"""Pydantic schemas for the Alert Rule dry-run/backtest API. See
app.services.alert_rule_backtest_service for the actual replay logic.
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class AlertRuleDryRunRequest(BaseModel):
    """Ad-hoc rule parameters to backtest -- mirrors AlertRuleCreate's
    metric/operator/threshold/scope/cooldown fields, but doesn't require
    (or create) a saved AlertRule, so a threshold can be sanity-checked
    while it's still being drafted in the UI."""

    metric: str
    operator: str
    threshold: float
    cooldown_seconds: int = 300
    scope_vendor: str | None = None
    scope_site: str | None = None
    scope_device_role: str | None = None
    # 1h .. 30d(-ish, capped server-side to whatever VictoriaMetrics
    # actually retains -- see alert_rule_backtest_service.MAX_LOOKBACK_HOURS).
    lookback_hours: int = Field(168, ge=1, le=24 * 90)


class AlertRuleFiringRead(BaseModel):
    device_id: uuid.UUID
    hostname: str
    fired_at: datetime
    cleared_at: datetime | None = None
    duration_seconds: float | None = None
    peak_value: float


class AlertRuleDeviceSummaryRead(BaseModel):
    device_id: uuid.UUID
    hostname: str
    firing_count: int
    suppressed_by_cooldown_count: int
    total_seconds_breached: float
    samples_evaluated: int


class AlertRuleDryRunResponse(BaseModel):
    supported: bool
    unsupported_reason: str | None = None

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

    # Most-recent-first, capped -- see
    # alert_rule_backtest_service.MAX_FIRINGS_RETURNED.
    firings: list[AlertRuleFiringRead] = []
    # Sorted busiest-device-first.
    per_device: list[AlertRuleDeviceSummaryRead] = []

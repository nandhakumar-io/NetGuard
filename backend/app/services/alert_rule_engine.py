"""Evaluates operator-defined AlertRule rows (app.api.alert_rules) against
a device's live poll data.

Until this module existed, AlertRule was CRUD-only: creating/toggling a
rule in the Alert Center UI persisted it, but nothing ever *read* it back
during a poll, so no custom rule could ever actually fire -- only the
separate hardcoded thresholds in snmp_service.evaluate_thresholds() did.
This closes that gap by hooking into the same per-device poll path
(metrics_service._raise_alerts) right after evaluate_thresholds(), using
the same SnmpMetrics sample so both paths look at identical numbers, and
funnels every breach through alert_service.raise_alert() so custom-rule
alerts dedup, escalate, and clear exactly like built-in ones.

Scope filters (scope_vendor / scope_site / scope_device_role) are matched
case-insensitively and only constrain when set -- a rule with all three
NULL applies fleet-wide, matching AlertRuleCreate's documented "NULL =
all devices" contract.

Cooldown: `cooldown_seconds` gates *re-firing* a rule that just cleared,
not ongoing occurrences of a still-active breach (those already dedup via
raise_alert's existing-unresolved-alert-with-same-category lookup). A
rule whose most recent alert for this device resolved less than
cooldown_seconds ago is skipped, so a metric oscillating right at the
threshold can't reopen/re-notify on every single poll.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.alert import Alert, AlertSource
from app.models.alert_rule import AlertRule
from app.models.device import Device
from app.services import alert_service
from app.services.snmp_service import SnmpMetrics

_OPERATORS = {
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "eq": lambda a, b: a == b,
}

# Custom-rule alerts get their own category namespace ("Custom Rule: X")
# so they never collide with -- or get silently deduped against -- the
# built-in category names evaluate_thresholds() uses (e.g. "High CPU").
# Both a built-in threshold and a custom rule can legitimately fire on
# the same underlying metric at the same time (an operator may want a
# tighter/looser custom threshold layered on top of the built-in one).
CUSTOM_RULE_CATEGORY_PREFIX = "Custom Rule: "


def _down_interface_count(metrics: SnmpMetrics) -> int:
    return sum(1 for iface in metrics.per_interface if iface.get("status") == "down")


def _metric_value(rule: AlertRule, metrics: SnmpMetrics) -> float | None:
    """Reads the value `rule.metric` refers to off this poll's
    SnmpMetrics, or None if that metric wasn't collected this poll
    (unsupported OID on this device, walk failed, etc) -- callers skip
    evaluation entirely in that case rather than comparing against a
    fabricated 0/None, same "don't invent data we don't have" posture as
    the rest of the SNMP pipeline.
    """
    metric = rule.metric.value if hasattr(rule.metric, "value") else rule.metric
    if metric == "cpu":
        return metrics.cpu_utilization_pct
    if metric == "memory":
        return metrics.memory_utilization_pct
    if metric == "bandwidth":
        return metrics.interface_utilization_pct
    if metric == "temperature":
        return metrics.temperature_celsius
    if metric == "uptime":
        return float(metrics.uptime_seconds) if metrics.uptime_seconds is not None else None
    if metric == "interface_errors":
        return float(metrics.interface_errors) if metrics.interface_errors is not None else None
    if metric == "interface_down_count":
        return float(_down_interface_count(metrics))
    if metric == "fan_failure":
        return 1.0 if metrics.fan_status == "failed" else 0.0
    if metric == "power_supply_failure":
        return 1.0 if metrics.power_supply_status == "failed" else 0.0
    return None


def _scope_matches(rule: AlertRule, device: Device) -> bool:
    if rule.scope_vendor and (device.vendor is None or rule.scope_vendor.lower() != str(device.vendor.value).lower()):
        return False
    if rule.scope_site and (device.site is None or rule.scope_site.lower() != device.site.lower()):
        return False
    if rule.scope_device_role and (
        device.device_role is None or rule.scope_device_role.lower() != device.device_role.lower()
    ):
        return False
    return True


def _in_cooldown(db: Session, device_id, category: str, cooldown_seconds: int, now: datetime) -> bool:
    if cooldown_seconds <= 0:
        return False
    last = (
        db.query(Alert)
        .filter(Alert.device_id == device_id, Alert.category == category, Alert.resolved == True)
        .order_by(Alert.resolved_at.desc())
        .first()
    )
    if last is None or last.resolved_at is None:
        return False
    resolved_at = last.resolved_at
    if resolved_at.tzinfo is None:
        resolved_at = resolved_at.replace(tzinfo=timezone.utc)
    return (now - resolved_at).total_seconds() < cooldown_seconds


def evaluate_rules(db: Session, device: Device, metrics: SnmpMetrics) -> None:
    """Runs every enabled AlertRule against this poll's metrics for
    `device`, raising/clearing Alert rows as needed. Best-effort per
    rule -- one rule with a bad metric/operator combo (shouldn't happen
    given the CRUD API's validation, but data can outlive code) is
    skipped rather than aborting evaluation of the rest.
    """
    if not metrics.reachable:
        return  # evaluate_thresholds() already raises "Device Unreachable"; nothing else is measurable this poll.

    rules = db.query(AlertRule).filter(AlertRule.enabled == True).all()
    if not rules:
        return

    now = datetime.now(timezone.utc)
    for rule in rules:
        try:
            if not _scope_matches(rule, device):
                continue
            value = _metric_value(rule, metrics)
            if value is None:
                continue
            op_fn = _OPERATORS.get(rule.operator.value if hasattr(rule.operator, "value") else rule.operator)
            if op_fn is None:
                continue

            category = f"{CUSTOM_RULE_CATEGORY_PREFIX}{rule.name}"
            breached = op_fn(value, rule.threshold)

            if breached:
                if _in_cooldown(db, device.id, category, rule.cooldown_seconds, now):
                    continue
                severity = rule.severity.value if hasattr(rule.severity, "value") else rule.severity
                alert, is_new = alert_service.raise_alert(
                    db,
                    device_id=device.id,
                    severity=severity,
                    source=AlertSource.HEALTH_POLL,
                    category=category,
                    message=f"{device.hostname}: {rule.name} ({rule.metric.value if hasattr(rule.metric, 'value') else rule.metric} {rule.operator.value if hasattr(rule.operator, 'value') else rule.operator} {rule.threshold}, observed {value})",
                )
                if severity == "critical" and is_new:
                    from app.services import notification_service

                    notification_service.notify(
                        event=category, message=alert.message, severity=severity, alert_id=alert.id
                    )
            else:
                alert_service.auto_resolve(
                    db, device_id=device.id, category=category, note=f"{device.hostname}: {rule.name} no longer breached"
                )
        except Exception:
            # Never let one malformed/legacy rule take down evaluation of
            # every other rule, or the poll itself.
            continue

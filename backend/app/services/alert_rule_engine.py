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
    if metric == "trunk_port_down":
        return float(metrics.trunk_ports_down) if metrics.trunk_ports_down is not None else None
    if metric == "sfp_port_down":
        return float(metrics.sfp_ports_down) if metrics.sfp_ports_down is not None else None
    if metric == "route_unreachable":
        return None if metrics.route_unreachable is None else (1.0 if metrics.route_unreachable else 0.0)
    if metric == "ping_packet_loss_pct":
        return metrics.ping_packet_loss_pct
    # Routing-protocol adjacency and LACP bundling -- see
    # snmp_service.walk_ospf_neighbors/walk_bgp_peers/walk_lacp_aggregates
    # and metrics_service._populate_link_metrics for how/when these get
    # collected.
    if metric == "ospf_neighbor_down":
        return float(metrics.ospf_neighbors_down) if metrics.ospf_neighbors_down is not None else None
    if metric == "bgp_session_down":
        return float(metrics.bgp_sessions_down) if metrics.bgp_sessions_down is not None else None
    if metric == "lacp_member_down":
        return float(metrics.lacp_degraded_channels) if metrics.lacp_degraded_channels is not None else None
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

    # Tenant scoping: previously this queried every AlertRule regardless
    # of tenant, so a tenant's custom rule fired against every OTHER
    # tenant's devices too -- the CRUD API (app.api.alert_rules) already
    # scoped reads/writes correctly, but nothing scoped evaluation. A
    # device's rules are the global (tenant_id IS NULL) ones plus its own
    # tenant's, same "global unless scoped" convention as everywhere else.
    #
    # AlertRule.enabled == False rows are still fetched here on purpose --
    # a disabled row is how a tenant's parent_rule_id override records
    # "suppress this global rule for us" (see the override resolution
    # below); excluding disabled rows outright would silently drop that
    # suppression and let the global rule fire anyway.
    candidate_rules = (
        db.query(AlertRule)
        .filter((AlertRule.tenant_id == device.tenant_id) | (AlertRule.tenant_id.is_(None)))
        .all()
    )
    if not candidate_rules:
        return

    # Override resolution: a tenant rule with parent_rule_id set replaces
    # the global rule it points at entirely for this device -- whether
    # that means a different threshold (tenant rule enabled=True) or full
    # suppression (tenant rule enabled=False, in which case neither rule
    # fires). A tenant rule with no parent_rule_id is additive, same as
    # before this column existed.
    overridden_ids = {
        rule.parent_rule_id
        for rule in candidate_rules
        if rule.tenant_id is not None and rule.parent_rule_id is not None
    }
    rules = [
        rule
        for rule in candidate_rules
        if rule.enabled and rule.id not in overridden_ids
    ]
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
                # Notification fan-out now happens inside
                # alert_service.raise_alert itself (all severities, not
                # just critical) -- see alert_service._dispatch_notification.
            else:
                alert_service.auto_resolve(
                    db, device_id=device.id, category=category, note=f"{device.hostname}: {rule.name} no longer breached"
                )
        except Exception:
            # Never let one malformed/legacy rule take down evaluation of
            # every other rule, or the poll itself.
            continue


def evaluate_ap_rules(db: Session, controller_device: Device) -> None:
    """Runs enabled AlertRules using the AP_CHANNEL_UTIL_PCT / AP_NOISE_DBM
    metrics against every WirelessAP under `controller_device`, right
    after a wireless poll. Deliberately separate from evaluate_rules()
    above: those metrics come off a Device's own SnmpMetrics, one sample
    per device per poll, while these come off a *per-AP* row -- a WLC
    with 40 APs needs up to 40 independent evaluations per poll, one per
    AP, each with its own alert category so a degraded AP doesn't get
    lost inside (or falsely clear) a WLC-wide alert.

    Only fires on rules scoped for this: scope_vendor/scope_site match
    against the AP itself (site) and its vendor string, not the
    controller Device, since a single WLC can front APs from more than
    one vendor/site in practice (manually-added APs aside from the
    polled ones, or a multi-site AireOS deployment). scope_device_role
    has no AP equivalent and is ignored for these two metrics.
    """
    from app.models.wireless import WirelessAP

    candidate_rules = (
        db.query(AlertRule)
        .filter(
            (AlertRule.tenant_id == controller_device.tenant_id) | (AlertRule.tenant_id.is_(None)),
            AlertRule.metric.in_(["ap_channel_util_pct", "ap_noise_dbm"]),
        )
        .all()
    )
    if not candidate_rules:
        return

    overridden_ids = {
        rule.parent_rule_id
        for rule in candidate_rules
        if rule.tenant_id is not None and rule.parent_rule_id is not None
    }
    rules = [rule for rule in candidate_rules if rule.enabled and rule.id not in overridden_ids]
    if not rules:
        return

    aps = db.query(WirelessAP).filter(WirelessAP.controller_device_id == controller_device.id).all()
    if not aps:
        return

    now = datetime.now(timezone.utc)
    for ap in aps:
        for rule in rules:
            try:
                if rule.scope_vendor and rule.scope_vendor.lower() != (ap.vendor or "").lower():
                    continue
                if rule.scope_site and (ap.site is None or rule.scope_site.lower() != ap.site.lower()):
                    continue

                metric = rule.metric.value if hasattr(rule.metric, "value") else rule.metric
                if metric == "ap_channel_util_pct":
                    readings = [v for v in (ap.channel_util_2g, ap.channel_util_5g) if v is not None]
                else:  # ap_noise_dbm -- noise is dBm, "worse" means higher/less-negative
                    readings = [v for v in (ap.noise_2g, ap.noise_5g) if v is not None]
                if not readings:
                    continue
                value = float(max(readings))

                op_fn = _OPERATORS.get(rule.operator.value if hasattr(rule.operator, "value") else rule.operator)
                if op_fn is None:
                    continue

                ap_label = ap.ap_name or ap.ap_index or str(ap.id)
                category = f"{CUSTOM_RULE_CATEGORY_PREFIX}{rule.name} ({ap_label})"
                breached = op_fn(value, rule.threshold)

                if breached:
                    if _in_cooldown(db, controller_device.id, category, rule.cooldown_seconds, now):
                        continue
                    severity = rule.severity.value if hasattr(rule.severity, "value") else rule.severity
                    alert_service.raise_alert(
                        db,
                        device_id=controller_device.id,
                        severity=severity,
                        source=AlertSource.HEALTH_POLL,
                        category=category,
                        message=(
                            f"{controller_device.hostname}: AP {ap_label} -- {rule.name} "
                            f"({metric} {rule.operator.value if hasattr(rule.operator, 'value') else rule.operator} "
                            f"{rule.threshold}, observed {value})"
                        ),
                    )
                else:
                    alert_service.auto_resolve(
                        db,
                        device_id=controller_device.id,
                        category=category,
                        note=f"AP {ap_label}: {rule.name} no longer breached",
                    )
            except Exception:
                continue

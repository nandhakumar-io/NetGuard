"""SNMP Health Dashboard: persistence + query layer on top of app.services.snmp_service.

snmp_service.poll_health() does the actual SNMP GET/WALK work and returns a
plain SnmpMetrics dataclass -- it doesn't know about the database. This
module is the glue: it runs a poll for a Device, turns the raw cumulative
interface counters into a utilization % (by diffing against the previous
DeviceMetric row), computes the health score/color, writes one DeviceMetric
row per poll, raises Alerts for any threshold breach, and fires
notifications for anything critical -- then exposes the read-side queries
(latest, history, fleet/device health summaries) the Metrics APIs use.
"""
import datetime
import uuid

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.alert import AlertSource
from app.models.device import Device, DeviceStatus
from app.models.device_metric import DeviceMetric, HealthColor
from app.models.device_status_history import DeviceStatusHistory
from app.models.interface_status import InterfaceOperStatus, InterfaceStatus
from app.services import (
    alert_service,
    credential_service,
    notification_service,
    snmp_service,
)
from app.services.snmp_service import SnmpMetrics


class SnmpNotConfiguredError(Exception):
    """Raised when a device isn't set up for SNMP polling."""


def build_snmp_auth(device: Device) -> "snmp_service.SnmpAuthConfig":
    """Public entry point for _build_snmp_auth -- used by
    app.api.devices' SNMP credentials test-connection endpoint, which
    needs the same v1/v2c/v3 credential resolution as poll_device()
    without actually running a full health poll.
    """
    return _build_snmp_auth(device)


def _build_snmp_auth(device: Device) -> "snmp_service.SnmpAuthConfig":
    """Builds the SnmpAuthConfig poll_health needs from a device row +
    the credential store -- v1/v2c resolves the community string, v3
    resolves username/USM params and raises CredentialNotFoundError with
    a specific, actionable message if a security-level-required secret
    (auth key for authNoPriv/authPriv, priv key for authPriv) is missing.
    """
    version = device.snmp_version.value
    port = device.snmp_port or 161

    if version in ("v1", "v2c"):
        community = credential_service.get_snmp_community(device)
        return snmp_service.SnmpAuthConfig(version=version, community=community, port=port)

    # v3
    if not device.snmp_username:
        raise credential_service.CredentialNotFoundError(
            f"Device '{device.hostname}' is set to SNMPv3 but has no snmp_username configured."
        )
    security_level = device.snmp_security_level.value if device.snmp_security_level else "noAuthNoPriv"
    # Default to SHA/AES128 when the security level requires auth/priv
    # but the operator hasn't explicitly picked a protocol yet (the
    # SnmpCredentialsModal only sends snmp_username + security_level in
    # the first "Save Version & Continue" step; protocol selectors are
    # added below in the frontend fix but we guard against None here too
    # so the poll-on-enable path never crashes with AttributeError).
    auth_protocol = device.snmp_auth_protocol.value if device.snmp_auth_protocol else (
        "SHA" if security_level in ("authNoPriv", "authPriv") else None
    )
    priv_protocol = device.snmp_priv_protocol.value if device.snmp_priv_protocol else (
        "AES128" if security_level == "authPriv" else None
    )
    auth_key = credential_service.get_snmp_v3_auth_key(device)
    priv_key = credential_service.get_snmp_v3_priv_key(device)

    if security_level in ("authNoPriv", "authPriv") and not auth_key:
        raise credential_service.CredentialNotFoundError(
            f"Device '{device.hostname}' security level is {security_level} but has no SNMPv3 auth key configured."
        )
    if security_level == "authPriv" and not priv_key:
        raise credential_service.CredentialNotFoundError(
            f"Device '{device.hostname}' security level is authPriv but has no SNMPv3 privacy key configured."
        )

    return snmp_service.SnmpAuthConfig(
        version="v3",
        port=port,
        username=device.snmp_username,
        security_level=security_level,
        auth_protocol=auth_protocol,
        auth_key=auth_key,
        priv_protocol=priv_protocol,
        priv_key=priv_key,
    )


def _compute_interface_utilization(
    metrics: SnmpMetrics, previous: DeviceMetric | None, interval_seconds: float
) -> float | None:
    """Interface Statistics panel: SNMP octet counters are cumulative
    since boot, not instantaneous, so utilization has to be derived from
    the delta between this poll and the previous one, divided by the
    elapsed time and the link's max throughput (ifHighSpeed).

    Returns None (rather than 0) when there's no prior sample to diff
    against, when the counters reset (device rebooted -- delta negative),
    or when speed is unknown -- all of these mean "can't compute yet", not
    "0% utilized".
    """
    if metrics.interface_octets_total is None or metrics.interface_speed_bps is None:
        return None
    if metrics.interface_speed_bps <= 0:
        return None
    if previous is None or previous.interface_octets_total is None:
        return None
    if interval_seconds <= 0:
        return None

    delta_octets = metrics.interface_octets_total - previous.interface_octets_total
    if delta_octets < 0:
        # Counter reset (reboot, counter wrap) -- not a valid delta.
        return None

    bits_transferred = delta_octets * 8
    bps = bits_transferred / interval_seconds
    utilization = round((bps / metrics.interface_speed_bps) * 100, 1)
    return max(0.0, min(100.0, utilization))


def _raise_alerts(db: Session, device: Device, metrics: SnmpMetrics) -> None:
    """Turns snmp_service.evaluate_thresholds() findings into Alert rows
    (Alert Engine) and fans critical ones out to Slack/Teams on first
    occurrence. Dedup-aware (alert_service.raise_alert): a still-active
    breach found on every subsequent poll updates the same standing alert
    rather than piling up a fresh duplicate row each time -- previously
    every poll unconditionally inserted a new Alert, which made "Clear
    Alerts" look broken since the next poll immediately recreated
    whatever had just been cleared. Best-effort: notification failures
    never block the poll (notification_service already swallows its own
    errors).
    """
    if metrics.reachable:
        # SNMP just answered -- if a prior poll had this device flagged
        # "Device Unreachable" via SNMP, that's over now. (The ping-based
        # "Device Unreachable" raised by reachability_service is cleared
        # independently there, on its own next successful ping.)
        alert_service.auto_resolve(
            db, device_id=device.id, category="Device Unreachable", note=f"{device.hostname}: SNMP poll succeeded again"
        )

    for severity, category, message in snmp_service.evaluate_thresholds(metrics):
        alert, is_new = alert_service.raise_alert(
            db,
            device_id=device.id,
            severity=severity,
            source=AlertSource.HEALTH_POLL,
            category=category,
            message=f"{device.hostname}: {message}",
        )
        if severity == "critical" and is_new:
            notification_service.notify(
                event=category, message=f"{device.hostname}: {message}", severity=severity
            )


def _sync_interface_status(db: Session, device: Device, metrics: SnmpMetrics) -> None:
    """Diffs this poll's per-interface oper status against the latest
    known InterfaceStatus row for each (device, ifIndex) and, on a real
    change, writes a new history row plus raises/auto-resolves the
    per-port "Interface Down" alert -- the NOC-dashboard-facing half of
    port monitoring (the other half, walking the SNMP table itself, is
    snmp_service.walk_interface_stats).

    Category is scoped to the specific interface (f"Interface Down:
    {descr}") rather than a single shared "Interface Down" category per
    device, so multiple down ports on the same device show as distinct,
    individually-resolvable alerts instead of one alert whose message
    keeps getting overwritten by whichever port polled last.
    """
    if not metrics.per_interface:
        return

    for entry in metrics.per_interface:
        if_index = str(entry.get("if_index"))
        if_descr = entry.get("if_descr") or f"if{if_index}"
        new_status = InterfaceOperStatus.DOWN if entry.get("status") == "down" else InterfaceOperStatus.UP

        latest = (
            db.query(InterfaceStatus)
            .filter(InterfaceStatus.device_id == device.id, InterfaceStatus.if_index == if_index)
            .order_by(InterfaceStatus.changed_at.desc())
            .first()
        )

        if latest is not None and latest.status == new_status:
            # No change -- don't spam a new history row every poll, just
            # keep whatever description we last saw (ifDescr essentially
            # never changes poll-to-poll, but stay defensive).
            continue

        db.add(
            InterfaceStatus(
                device_id=device.id,
                if_index=if_index,
                if_descr=if_descr,
                status=new_status,
                previous_status=latest.status if latest is not None else None,
            )
        )

        category = f"Interface Down: {if_descr}"

        if new_status == InterfaceOperStatus.DOWN:
            # A device's very first-ever poll finding a port already down
            # still alerts (there's nothing "newly down" to compare
            # against, but an operationally-down port is worth surfacing
            # regardless of whether we caught the actual transition).
            alert, is_new = alert_service.raise_alert(
                db,
                device_id=device.id,
                severity="critical",
                source=AlertSource.HEALTH_POLL,
                category=category,
                message=f"{device.hostname}: interface {if_descr} is down",
            )
            if is_new:
                notification_service.notify(
                    event="Interface Down",
                    message=f"{device.hostname}: interface {if_descr} is down",
                    severity="critical",
                )
        elif latest is not None:
            # Only auto-resolve on a genuine down->up recovery, not on the
            # very first poll ever seeing this port (nothing to clear).
            alert_service.auto_resolve(
                db,
                device_id=device.id,
                category=category,
                note=f"{device.hostname}: interface {if_descr} is back up",
            )


def _check_device_restart(db: Session, device: Device, metrics: SnmpMetrics, previous: DeviceMetric | None) -> None:
    """Detects a reboot (sysUpTime counter reset to a value lower than
    the previous poll's) and raises a "Device Restart" alert -- the same
    category name app.services.snmp_service.classify_trap already uses
    for inbound coldStart/warmStart traps, so the Alert Center treats a
    polling-detected reboot identically to a trap-reported one.

    A small grace window (uptime under 5 minutes) avoids false "restart"
    alerts firing again on every subsequent poll after the real one --
    without it, a device that stays freshly-booted across two polls (poll
    interval shorter than 5 minutes) would look like it rebooted twice.
    """
    if metrics.uptime_seconds is None or previous is None or previous.uptime_seconds is None:
        return
    if metrics.uptime_seconds < previous.uptime_seconds and metrics.uptime_seconds < 300:
        alert, is_new = alert_service.raise_alert(
            db,
            device_id=device.id,
            severity="warning",
            source=AlertSource.HEALTH_POLL,
            category="Device Restart",
            message=f"{device.hostname}: device rebooted (uptime reset, now {metrics.uptime_seconds}s)",
        )
        if is_new:
            notification_service.notify(
                event="Device Restart",
                message=f"{device.hostname}: device rebooted",
                severity="warning",
            )


# Metric family -> (SnmpMetrics attribute, Device freshness-column attribute).
# Interface utilization deliberately uses the *computed* pct (metrics.
# interface_utilization_pct, set just above this dict's call site) rather
# than the raw octet counters, since a device needs two consecutive good
# polls before that pct exists at all -- stamping on the raw counters
# would mark the metric "fresh" one poll before it's actually usable.
_FRESHNESS_FIELDS = (
    ("cpu_utilization_pct", "last_cpu_success_at"),
    ("memory_utilization_pct", "last_memory_success_at"),
    ("interface_utilization_pct", "last_interface_success_at"),
    ("temperature_celsius", "last_temperature_success_at"),
    ("fan_status", "last_fan_success_at"),
    ("power_supply_status", "last_power_success_at"),
)


def _stamp_metric_freshness(device: Device, metrics: SnmpMetrics) -> None:
    """Data Completeness: updates each per-metric last-successful-read
    column on `device` for whichever readings actually came back non-None
    on this poll -- a metric that failed to resolve this time just keeps
    its previous timestamp (so it visibly falls behind) instead of being
    cleared or stamped with "now" like a good reading would be.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    for metrics_attr, device_attr in _FRESHNESS_FIELDS:
        if getattr(metrics, metrics_attr, None) is not None:
            setattr(device, device_attr, now)


def metric_freshness(device: Device) -> dict:
    """Per-metric last-successful-read timestamps for one device, as ISO
    strings (None if that metric has never once resolved) -- what the
    Health Dashboard card/detail view uses to show a per-metric "stale"
    badge instead of relying solely on the device-level health color."""
    return {
        "cpu": device.last_cpu_success_at.isoformat() if device.last_cpu_success_at else None,
        "memory": device.last_memory_success_at.isoformat() if device.last_memory_success_at else None,
        "interface": device.last_interface_success_at.isoformat() if device.last_interface_success_at else None,
        "temperature": device.last_temperature_success_at.isoformat() if device.last_temperature_success_at else None,
        "fan": device.last_fan_success_at.isoformat() if device.last_fan_success_at else None,
        "power": device.last_power_success_at.isoformat() if device.last_power_success_at else None,
    }


# A metric is flagged "stale" for the fleet-health rollup once its last
# successful read is older than this, even if the device's overall
# health_color is still green from whatever readings *did* come back.
# Matched to the default SNMP poll cadence (see settings.SNMP_POLL_INTERVAL_
# SECONDS) with generous headroom -- a couple of missed polls in a row is
# noise (one bad walk), several in a row is a real gap worth surfacing.
STALE_METRIC_THRESHOLD = datetime.timedelta(hours=1)


# Short metric-family name (matches metric_freshness()'s keys, and what
# the frontend badges on) -> Device freshness-column attribute.
_STALE_CHECK_FIELDS = (
    ("cpu", "last_cpu_success_at"),
    ("memory", "last_memory_success_at"),
    ("interface", "last_interface_success_at"),
    ("temperature", "last_temperature_success_at"),
    ("fan", "last_fan_success_at"),
    ("power", "last_power_success_at"),
)


def stale_metric_names(device: Device) -> list[str]:
    """Names (matching MetricFreshness's fields, e.g. "interface") of
    metric families that have resolved at least once for this device but
    are now lagging significantly behind its own most recent poll -- e.g.
    CPU/mem keep advancing every poll while the interface table has
    quietly stopped resolving. A metric that has *never* resolved (still
    None) is not included: for hardware without a given sensor (e.g. a
    lab image with no fan/power MIBs) that's expected and permanent, not
    a regression. Devices never polled at all return [] too (pre-existing
    empty-Health-tab case, not a "gone stale" case).
    """
    if device.last_snmp_poll_at is None:
        return []
    stale: list[str] = []
    for family, device_attr in _STALE_CHECK_FIELDS:
        stamped = getattr(device, device_attr, None)
        if stamped is not None and (device.last_snmp_poll_at - stamped) > STALE_METRIC_THRESHOLD:
            stale.append(family)
    return stale


def has_stale_metric(device: Device) -> bool:
    """True if this SNMP-monitored device has at least one metric family
    that used to resolve successfully but has fallen behind the device's
    own recent polls -- the Data Completeness signal the fleet health
    strip's "N devices with stale metrics" badge is built from.
    """
    return bool(stale_metric_names(device))


def poll_device(db: Session, device: Device) -> DeviceMetric:
    """Runs one SNMP health poll for `device`, persists the result as a new
    DeviceMetric row, raises any threshold-breach Alerts, and returns the
    row. This is the single entry point both the Celery poll task and the
    on-demand "poll now" API call go through, so both paths get identical
    behavior (same alerting, same interface-utilization math).

    Also stamps device.last_snmp_poll_at/last_snmp_poll_error on every
    attempt -- success, "device didn't respond" (a poll that completes
    without exception but comes back with nothing, e.g. an unreachable
    device -- see the all-None SnmpMetrics case below), and a hard
    failure before any SNMP call went out (not configured / credentials
    missing). Previously none of that was visible anywhere but the
    server log, so a device with an empty Health tab looked identical
    whether it had never been polled, had incomplete SNMPv3 credentials,
    or was simply unreachable.
    """
    device.last_snmp_poll_at = datetime.datetime.now(datetime.timezone.utc)

    if not device.supports_snmp or not device.snmp_version:
        device.last_snmp_poll_error = "SNMP monitoring is not enabled for this device"
        db.commit()
        raise SnmpNotConfiguredError(f"Device '{device.hostname}' is not configured for SNMP polling")

    try:
        auth = _build_snmp_auth(device)
    except credential_service.CredentialNotFoundError as exc:
        device.last_snmp_poll_error = str(exc)
        db.commit()
        raise

    previous = (
        db.query(DeviceMetric)
        .filter(DeviceMetric.device_id == device.id)
        .order_by(DeviceMetric.polled_at.desc())
        .first()
    )
    interval_seconds = (
        (datetime.datetime.now(datetime.timezone.utc) - previous.polled_at).total_seconds()
        if previous is not None
        else 0.0
    )

    from app.core.config import settings

    metrics = snmp_service.poll_health(
        device.ip_address,
        auth,
        timeout=settings.SNMP_TIMEOUT_SECONDS,
        vendor=device.vendor.value if hasattr(device.vendor, "value") else str(device.vendor),
    )
    metrics.interface_utilization_pct = _compute_interface_utilization(metrics, previous, interval_seconds)

    score, color = snmp_service.compute_health_score(metrics)

    # poll_health doesn't raise on an unreachable device -- it returns an
    # all-None SnmpMetrics (sysUpTime is the one OID every SNMP agent
    # answers, so its absence means nothing came back at all) and lets
    # evaluate_thresholds() below turn that into an Alert. That's still a
    # real, worth-surfacing failure for last_snmp_poll_error even though
    # poll_device itself completes "successfully" (a DeviceMetric row is
    # still written, just an empty one).
    device.last_snmp_poll_error = (
        None if metrics.uptime_seconds is not None else "Device did not respond to SNMP GET (sysUpTime)"
    )

    # Keep Device.status in sync with reality the moment we have direct
    # evidence, rather than making the operator wait for the next
    # independent reachability_service ping sweep (REACHABILITY_POLL_
    # INTERVAL_SECONDS) to catch up. A device that just answered dozens
    # of SNMP GETs is unambiguously online -- there's no reason its
    # status badge should still say UNKNOWN/OFFLINE until a separate
    # subsystem gets around to pinging it. We only ever upgrade status
    # here, never downgrade to OFFLINE on a failed SNMP poll: SNMP not
    # responding (bad community string, ACL, agent disabled) is weak
    # evidence of the device being down, and that call is left to the
    # ping-based reachability sweep, which is a more direct signal for
    # "is it actually offline."
    if metrics.uptime_seconds is not None:
        new_status = DeviceStatus.DEGRADED if color in ("yellow", "red") else DeviceStatus.ONLINE
        if device.status != new_status:
            db.add(DeviceStatusHistory(device_id=device.id, status=new_status, previous_status=device.status))
        device.status = new_status

    _stamp_metric_freshness(device, metrics)

    row = DeviceMetric(
        device_id=device.id,
        cpu_utilization_pct=metrics.cpu_utilization_pct,
        memory_utilization_pct=metrics.memory_utilization_pct,
        interface_utilization_pct=metrics.interface_utilization_pct,
        interface_errors=metrics.interface_errors,
        temperature_celsius=metrics.temperature_celsius,
        fan_status=metrics.fan_status,
        power_supply_status=metrics.power_supply_status,
        uptime_seconds=metrics.uptime_seconds,
        health_score=score,
        health_color=HealthColor(color),
        interface_octets_total=metrics.interface_octets_total,
        interface_speed_bps=metrics.interface_speed_bps,
    )
    db.add(row)

    _raise_alerts(db, device, metrics)
    _check_device_restart(db, device, metrics, previous)
    _sync_interface_status(db, device, metrics)

    db.commit()
    db.refresh(row)
    return row


def device_health(db: Session, device: Device) -> dict:
    """Latest health snapshot for one device (Health Dashboard card)."""
    latest = (
        db.query(DeviceMetric)
        .filter(DeviceMetric.device_id == device.id)
        .order_by(DeviceMetric.polled_at.desc())
        .first()
    )
    if latest is None:
        return {
            "device_id": device.id,
            "hostname": device.hostname,
            "health_score": None,
            "health_color": "unknown",
            "reachable": False,
            "latest_metric": None,
            "metric_freshness": metric_freshness(device),
            "stale_metrics": stale_metric_names(device),
        }
    return {
        "device_id": device.id,
        "hostname": device.hostname,
        "health_score": latest.health_score,
        "health_color": latest.health_color.value if latest.health_color else "unknown",
        "reachable": latest.health_score is not None and latest.health_score > 0,
        "latest_metric": latest,
        "metric_freshness": metric_freshness(device),
        "stale_metrics": stale_metric_names(device),
    }


def fleet_health_summary(db: Session, vendor: str | None = None) -> dict:
    """Fleet-wide rollup for the top of the Health Dashboard: how many
    devices are green/yellow/red right now, based on each device's most
    recent DeviceMetric row (one row per device, not one per poll).

    ``vendor`` optionally scopes the rollup to a single vendor (e.g.
    "juniper") so the Device Inventory page's vendor filter can turn this
    into a one-vendor fleet-health strip without a separate endpoint --
    see GET /metrics/health-summary?vendor=. Devices with at least one
    metric family that's fallen behind (see has_stale_metric) are counted
    separately so a fleet that's green-on-paper doesn't hide a data-
    completeness gap.
    """
    query = db.query(Device).filter(Device.supports_snmp.is_(True))
    if vendor:
        from app.models.device import DeviceVendor

        try:
            query = query.filter(Device.vendor == DeviceVendor(vendor.lower()))
        except ValueError:
            pass  # unknown vendor string -- fall through to an empty summary below
    snmp_devices = query.all()

    counts = {"green": 0, "yellow": 0, "red": 0, "unknown": 0}
    scores: list[int] = []
    stale_count = 0
    for device in snmp_devices:
        latest = (
            db.query(DeviceMetric)
            .filter(DeviceMetric.device_id == device.id)
            .order_by(DeviceMetric.polled_at.desc())
            .first()
        )
        if has_stale_metric(device):
            stale_count += 1
        if latest is None or latest.health_color is None:
            counts["unknown"] += 1
            continue
        counts[latest.health_color.value] += 1
        if latest.health_score is not None:
            scores.append(latest.health_score)

    return {
        "devices_monitored": len(snmp_devices),
        "green": counts["green"],
        "yellow": counts["yellow"],
        "red": counts["red"],
        "unknown": counts["unknown"],
        "average_health_score": round(sum(scores) / len(scores)) if scores else None,
        "devices_with_stale_metrics": stale_count,
    }


def _nines(pct: float) -> str:
    """Renders an availability percentage as the classic NOC "how many
    nines" label (99.9%, 99.95%, 99.99%, ...) instead of a flat rounded
    number, since that's the conventional way an availability SLA number
    gets talked about on a NOC dashboard. Caps the displayed precision at
    4 nines (99.99%) -- beyond that the extra digits aren't meaningful
    given this rollup's polling-interval-scale sampling resolution.
    """
    if pct >= 99.99:
        return "99.99%"
    if pct >= 99.9:
        return f"{pct:.2f}%"
    if pct >= 99:
        return f"{pct:.1f}%"
    return f"{pct:.1f}%"


def _as_aware_utc(dt: datetime.datetime) -> datetime.datetime:
    """Normalizes a DateTime(timezone=True) column value to a
    timezone-aware UTC datetime. Postgres round-trips these as aware, but
    SQLite (used in tests) drops the tzinfo on read -- comparing/
    subtracting a naive value against the aware `now`/`since` below raises
    TypeError, so every row read out of the DB in this rollup is coerced
    through here first.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def fleet_availability_summary(db: Session, hours: int = 24) -> dict:
    """Fleet-wide uptime rollup ("five nines" number) computed from
    DeviceStatusHistory: for each monitored device, what fraction of the
    window was it in an "available" state (ONLINE or DEGRADED -- reachable
    counts even if unhealthy; only OFFLINE/UNKNOWN count as down) versus
    unavailable.

    Time-weighted, not a snapshot: a device that was offline for 10
    minutes out of a 24h window is ~99.3% available for that window, not
    just "counted as down" or "counted as up" based on its current status.
    Devices with no history rows at all in the window are assumed to have
    held their current status for the whole window (nothing changed, so
    there was nothing to log) -- see the per-device loop below.

    Devices with `supports_snmp=False` and no reachability history are
    excluded from the rollup rather than silently reported as 100%
    available, since a device NetGuard has literally never checked
    shouldn't drag the number up.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    since = now - datetime.timedelta(hours=hours)

    devices = db.query(Device).all()
    per_device: list[dict] = []
    total_available_seconds = 0.0
    total_seconds = 0.0

    for device in devices:
        history = (
            db.query(DeviceStatusHistory)
            .filter(DeviceStatusHistory.device_id == device.id, DeviceStatusHistory.changed_at >= since)
            .order_by(DeviceStatusHistory.changed_at.asc())
            .all()
        )
        # The most recent transition *before* the window opened -- tells
        # us what status the device was actually in at `since`, so the
        # very first segment of the window isn't wrongly assumed
        # UNKNOWN/available.
        prior = (
            db.query(DeviceStatusHistory)
            .filter(DeviceStatusHistory.device_id == device.id, DeviceStatusHistory.changed_at < since)
            .order_by(DeviceStatusHistory.changed_at.desc())
            .first()
        )
        if not history and prior is None:
            # Never once observed changing status -- nothing to roll up
            # for this device (e.g. added but never polled/pinged yet).
            continue

        status_at_window_start = prior.status if prior is not None else (history[0].previous_status or device.status)
        window_seconds = (now - since).total_seconds()
        available_seconds = 0.0
        cursor = since
        current_status = status_at_window_start
        for row in history:
            changed_at = _as_aware_utc(row.changed_at)
            segment = (changed_at - cursor).total_seconds()
            if current_status in (DeviceStatus.ONLINE, DeviceStatus.DEGRADED):
                available_seconds += max(segment, 0.0)
            cursor = changed_at
            current_status = row.status
        tail_segment = (now - cursor).total_seconds()
        if current_status in (DeviceStatus.ONLINE, DeviceStatus.DEGRADED):
            available_seconds += max(tail_segment, 0.0)

        pct = (available_seconds / window_seconds * 100) if window_seconds > 0 else 100.0
        per_device.append({
            "device_id": device.id,
            "hostname": device.hostname,
            "availability_pct": round(pct, 3),
        })
        total_available_seconds += available_seconds
        total_seconds += window_seconds

    fleet_pct = (total_available_seconds / total_seconds * 100) if total_seconds > 0 else None
    worst = sorted(per_device, key=lambda d: d["availability_pct"])[:10]

    return {
        "window_hours": hours,
        "devices_in_rollup": len(per_device),
        "fleet_availability_pct": round(fleet_pct, 3) if fleet_pct is not None else None,
        "fleet_availability_label": _nines(fleet_pct) if fleet_pct is not None else "n/a",
        "worst_devices": worst,
    }


def unstable_devices(db: Session, hours: int = 24, limit: int = 10) -> list[dict]:
    """"Top flapping devices" NOC widget: combines three independent
    instability signals into one per-device score so an operator doesn't
    have to cross-reference three separate pages to notice a device is
    generally misbehaving:

      - reachability_flaps: DeviceStatusHistory transitions in the window
        (device dropping on/off the network -- flaky cabling, power,
        upstream link).
      - interface_flaps: InterfaceStatus transitions in the window, summed
        across all of the device's interfaces (a specific port bouncing --
        bad transceiver, duplex mismatch, STP topology change).
      - drift_events: ConfigDrift rows detected in the window (config
        changing out from under the baseline -- often *correlated* with
        the other two, e.g. someone flapping an interface administratively
        while troubleshooting, or a device reloading to a different
        startup-config).

    `instability_score` is a simple weighted sum (reachability flaps
    weighted highest since a whole device dropping off is the worst
    signal, drift weighted lowest since a single config change is a much
    weaker instability signal than either kind of flapping) -- good enough
    to rank/triage with, not a scientific SLA metric.
    """
    from app.models.config_drift import ConfigDrift

    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)

    reachability_counts: dict[uuid.UUID, int] = {}
    for device_id, count in (
        db.query(DeviceStatusHistory.device_id, func.count(DeviceStatusHistory.id))
        .filter(DeviceStatusHistory.changed_at >= since)
        .group_by(DeviceStatusHistory.device_id)
        .all()
    ):
        reachability_counts[device_id] = count

    interface_counts: dict[uuid.UUID, int] = {}
    for device_id, count in (
        db.query(InterfaceStatus.device_id, func.count(InterfaceStatus.id))
        .filter(InterfaceStatus.changed_at >= since)
        .group_by(InterfaceStatus.device_id)
        .all()
    ):
        interface_counts[device_id] = count

    drift_counts: dict[uuid.UUID, int] = {}
    for device_id, count in (
        db.query(ConfigDrift.device_id, func.count(ConfigDrift.id))
        .filter(ConfigDrift.detected_at >= since)
        .group_by(ConfigDrift.device_id)
        .all()
    ):
        drift_counts[device_id] = count

    device_ids = set(reachability_counts) | set(interface_counts) | set(drift_counts)
    if not device_ids:
        return []

    devices_by_id = {d.id: d for d in db.query(Device).filter(Device.id.in_(device_ids)).all()}

    rows = []
    for device_id in device_ids:
        device = devices_by_id.get(device_id)
        if device is None:
            continue
        reachability_flaps = reachability_counts.get(device_id, 0)
        interface_flaps = interface_counts.get(device_id, 0)
        drift_events = drift_counts.get(device_id, 0)
        score = (reachability_flaps * 5) + (interface_flaps * 2) + (drift_events * 1)
        if score == 0:
            continue
        rows.append({
            "device_id": device_id,
            "hostname": device.hostname,
            "reachability_flaps": reachability_flaps,
            "interface_flaps": interface_flaps,
            "drift_events": drift_events,
            "instability_score": score,
        })

    rows.sort(key=lambda r: r["instability_score"], reverse=True)
    return rows[:limit]


def metric_history(
    db: Session, device_id: uuid.UUID, hours: int = 24, limit: int = 500
) -> list[DeviceMetric]:
    """Historical Charts: chronological (oldest-first, so charting
    libraries don't need to reverse it) DeviceMetric rows for one device
    over the last `hours` hours, capped at `limit` points so a device
    polled every minute for weeks doesn't blow up one response payload.
    """
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    rows = (
        db.query(DeviceMetric)
        .filter(DeviceMetric.device_id == device_id, DeviceMetric.polled_at >= since)
        .order_by(DeviceMetric.polled_at.desc())
        .limit(limit)
        .all()
    )
    return list(reversed(rows))


def purge_old_metrics(db: Session, retention_days: int) -> int:
    """Housekeeping: deletes DeviceMetric rows older than the retention
    window so history tables don't grow unbounded. Returns rows deleted.
    """
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=retention_days)
    deleted = db.query(DeviceMetric).filter(DeviceMetric.polled_at < cutoff).delete(synchronize_session=False)
    db.commit()
    return deleted

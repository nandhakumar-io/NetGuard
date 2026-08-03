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
from app.models.device import Device
from app.models.device_metric import DeviceMetric, HealthColor
from app.services import alert_service, credential_service, notification_service, snmp_service
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
        }
    return {
        "device_id": device.id,
        "hostname": device.hostname,
        "health_score": latest.health_score,
        "health_color": latest.health_color.value if latest.health_color else "unknown",
        "reachable": latest.health_score is not None and latest.health_score > 0,
        "latest_metric": latest,
    }


def fleet_health_summary(db: Session) -> dict:
    """Fleet-wide rollup for the top of the Health Dashboard: how many
    devices are green/yellow/red right now, based on each device's most
    recent DeviceMetric row (one row per device, not one per poll).
    """
    snmp_devices = db.query(Device).filter(Device.supports_snmp.is_(True)).all()

    counts = {"green": 0, "yellow": 0, "red": 0, "unknown": 0}
    scores: list[int] = []
    for device in snmp_devices:
        latest = (
            db.query(DeviceMetric)
            .filter(DeviceMetric.device_id == device.id)
            .order_by(DeviceMetric.polled_at.desc())
            .first()
        )
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
    }


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
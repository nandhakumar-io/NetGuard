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

from app.models.alert import Alert, AlertSeverity, AlertSource
from app.models.device import Device
from app.models.device_metric import DeviceMetric, HealthColor
from app.services import credential_service, notification_service, snmp_service
from app.services.snmp_service import SnmpMetrics


class SnmpNotConfiguredError(Exception):
    """Raised when a device isn't set up for SNMP polling."""


def _resolve_community(device: Device) -> str:
    """SNMP v1/v2c community string, resolved the same way SSH/NETCONF
    secrets are (credential_service, env-backed secret store).
    """
    return credential_service.get_secret(
        device.snmp_community_ref, device=device, label="SNMP community"
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
    (Alert Engine) and fans critical ones out to Slack/Teams. Best-effort:
    notification failures never block the poll (notification_service
    already swallows its own errors).
    """
    for severity, category, message in snmp_service.evaluate_thresholds(metrics):
        alert = Alert(
            device_id=device.id,
            severity=AlertSeverity(severity),
            source=AlertSource.HEALTH_POLL,
            category=category,
            message=f"{device.hostname}: {message}",
        )
        db.add(alert)
        if severity == "critical":
            notification_service.notify(
                event=category, message=f"{device.hostname}: {message}", severity=severity
            )


def poll_device(db: Session, device: Device) -> DeviceMetric:
    """Runs one SNMP health poll for `device`, persists the result as a new
    DeviceMetric row, raises any threshold-breach Alerts, and returns the
    row. This is the single entry point both the Celery poll task and the
    on-demand "poll now" API call go through, so both paths get identical
    behavior (same alerting, same interface-utilization math).
    """
    if not device.supports_snmp or not device.snmp_version:
        raise SnmpNotConfiguredError(f"Device '{device.hostname}' is not configured for SNMP polling")

    community = _resolve_community(device)

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
        community,
        version=device.snmp_version.value,
        timeout=settings.SNMP_TIMEOUT_SECONDS,
    )
    metrics.interface_utilization_pct = _compute_interface_utilization(metrics, previous, interval_seconds)

    score, color = snmp_service.compute_health_score(metrics)

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
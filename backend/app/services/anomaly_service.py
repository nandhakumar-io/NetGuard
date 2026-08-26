"""Baseline-based anomaly detection.

app.services.snmp_service already raises alerts off *static* thresholds
(e.g. "CPU > 90%"). Those miss two common NOC-relevant patterns:

  1. Slow drift that never crosses a fixed line (a device that's
     historically idle at 5-10% CPU creeping up to a "fine by the
     threshold" 55% over weeks).
  2. Time-of-day-relative spikes (a device that's normally busy at 60%
     during business hours but is unexpectedly at 60% at 3am).

This service computes each device's own historical baseline for a
metric, bucketed by hour-of-day so "normal for 3am" and "normal for
2pm" aren't averaged together, and raises an ANOMALY-sourced alert when
the latest reading is a statistical outlier against that baseline --
independent of, and in addition to, snmp_service's static thresholds.

Deliberately reuses app.services.alert_service.raise_alert (not a
separate code path) so anomaly alerts get the same dedup-by-category,
maintenance-window suppression, and correlation behavior as every other
alert source.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.device import Device
from app.models.device_metric import DeviceMetric
from app.services import alert_service

# How far back to look when building a device's baseline. Long enough to
# smooth over a handful of noisy days, short enough that a genuine,
# sustained change in the device's normal operating pattern (a real
# capacity increase, a newly-added workload) ages out of the baseline
# instead of being flagged forever.
BASELINE_WINDOW_DAYS = 14

# Minimum number of historical samples in a given hour-of-day bucket
# before a baseline is considered trustworthy enough to alert against --
# below this, a "baseline" is really just noise (e.g. a device that's
# only been in inventory for two days).
MIN_SAMPLES_FOR_BASELINE = 8

# Standard deviations from the baseline mean before a reading counts as
# anomalous. Chosen deliberately looser than a typical 2-sigma statistics
# default -- this runs continuously against every metric on every device,
# and false-positive anomaly alerts erode NOC trust in the feature faster
# than missing a marginal one costs.
Z_SCORE_THRESHOLD = 3.0

# A metric needs at least this much absolute spread in its own history to
# be worth z-scoring at all -- a device whose CPU has sat at a rock-steady
# 4% for two weeks has a near-zero stddev, which would make a totally
# unremarkable 6% reading register as a huge z-score. Metrics below this
# floor are skipped rather than flagged.
MIN_STDDEV_FOR_METRIC = {
    "cpu_utilization_pct": 3.0,
    "memory_utilization_pct": 3.0,
    "interface_utilization_pct": 5.0,
}

_METRIC_LABELS = {
    "cpu_utilization_pct": "CPU utilization",
    "memory_utilization_pct": "memory utilization",
    "interface_utilization_pct": "interface utilization",
}


@dataclass
class AnomalyFinding:
    device_id: object
    metric: str
    latest_value: float
    baseline_mean: float
    baseline_stddev: float
    z_score: float
    sample_count: int


def _baseline_for_hour(
    db: Session, device_id, metric_column, hour: int, since: datetime
) -> tuple[float | None, float | None, int]:
    """Mean, stddev, and sample count for one metric/device/hour-of-day
    bucket over the trailing baseline window, excluding the metric's own
    NULLs (a device that doesn't report a given OID shouldn't drag its
    own baseline toward zero).
    """
    row = (
        db.query(
            func.avg(metric_column),
            func.stddev(metric_column),
            func.count(metric_column),
        )
        .filter(
            DeviceMetric.device_id == device_id,
            DeviceMetric.polled_at >= since,
            metric_column.isnot(None),
            func.extract("hour", DeviceMetric.polled_at) == hour,
        )
        .one()
    )
    mean, stddev, count = row
    return (float(mean) if mean is not None else None, float(stddev) if stddev is not None else None, count or 0)


def check_device_for_anomalies(db: Session, device: Device) -> list[AnomalyFinding]:
    """Compares a device's latest metric poll against its own hour-of-day
    baseline for each tracked metric and returns any anomalous findings.

    Read-only -- callers decide whether/how to alert on the result (see
    run_anomaly_detection_task in app.tasks, which raises an Alert per
    finding via alert_service.raise_alert).
    """
    latest = (
        db.query(DeviceMetric)
        .filter(DeviceMetric.device_id == device.id)
        .order_by(DeviceMetric.polled_at.desc())
        .first()
    )
    if latest is None:
        return []

    since = datetime.now(timezone.utc) - timedelta(days=BASELINE_WINDOW_DAYS)
    hour = latest.polled_at.hour if latest.polled_at else datetime.now(timezone.utc).hour

    findings: list[AnomalyFinding] = []
    for metric_name, min_stddev in MIN_STDDEV_FOR_METRIC.items():
        latest_value = getattr(latest, metric_name)
        if latest_value is None:
            continue

        metric_column = getattr(DeviceMetric, metric_name)
        mean, stddev, count = _baseline_for_hour(db, device.id, metric_column, hour, since)
        if mean is None or stddev is None or count < MIN_SAMPLES_FOR_BASELINE:
            continue
        if stddev < min_stddev:
            continue

        z_score = abs(latest_value - mean) / stddev
        if z_score >= Z_SCORE_THRESHOLD and latest_value > mean:
            # Only alert on the "worse than usual" direction (higher
            # utilization) -- a device that's unusually *idle* isn't a
            # NOC-actionable anomaly the way an unusual spike is.
            findings.append(
                AnomalyFinding(
                    device_id=device.id,
                    metric=metric_name,
                    latest_value=latest_value,
                    baseline_mean=mean,
                    baseline_stddev=stddev,
                    z_score=z_score,
                    sample_count=count,
                )
            )

    return findings


def raise_anomaly_alerts(db: Session, device: Device, findings: list[AnomalyFinding]) -> int:
    """Raises one alert per finding via the shared alert_service path.
    Returns the number of alerts raised/updated.
    """
    for finding in findings:
        label = _METRIC_LABELS.get(finding.metric, finding.metric)
        alert_service.raise_alert(
            db,
            device_id=device.id,
            severity="warning",
            source="anomaly",
            category=f"Anomalous {label}",
            message=(
                f"{label.capitalize()} is {finding.latest_value:.1f}%, which is "
                f"{finding.z_score:.1f} standard deviations above this device's "
                f"usual {finding.baseline_mean:.1f}% (±{finding.baseline_stddev:.1f}) "
                f"for this time of day, based on {finding.sample_count} samples "
                f"over the last {BASELINE_WINDOW_DAYS} days."
            ),
        )
    return len(findings)

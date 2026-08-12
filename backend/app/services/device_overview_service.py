"""Backs the unified "why is this device unhealthy" view (GET
/devices/{device_id}/overview).

Health, alerts, drift, syslog, and deployments/config-changes were all
separate pages that only ever showed one dimension of a device's state —
an engineer triaging an incident had to tab-hop between Health, Alert
Center, Drift, and Syslog Viewer, manually correlating timestamps in
their head to figure out "what changed right before this went red".
This module does that correlation once, server-side: it pulls the
recent record from each of those four sources for a single device and
merges them into one time-ordered timeline, so the device detail panel
can render a single answer instead of four disjoint tables.

Deliberately read-only / best-effort: nothing here writes data or
introduces a new source of truth. If a downstream table is empty
(e.g. no drift scans have ever run for this device) that source is
just absent from the timeline rather than raising.
"""
import datetime
import uuid

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.models.alert import Alert
from app.models.config_drift import ConfigDrift
from app.models.deployment import Deployment
from app.models.device import Device
from app.models.syslog_message import SyslogMessage, SyslogSeverity
from app.services import metrics_service

# Syslog is by far the highest-volume source (routine NOTICE/INFO/DEBUG
# chatter). Only pull ERROR-and-more-severe lines into the correlated
# timeline, same cutoff the syslog_service uses to decide what's
# "correlation-worthy" -- otherwise the timeline drowns in noise and the
# signal (the handful of lines that actually explain a health dip) gets
# buried.
TIMELINE_SYSLOG_MAX_SEVERITY = SyslogSeverity.ERROR
# SyslogSeverity is stored as a Postgres/SQLAlchemy Enum (by name, not by
# its underlying int value), so "<= ERROR" can't be expressed as a SQL
# comparison the way it could for a plain integer column -- build the
# explicit membership list once instead.
_NOTABLE_SYSLOG_SEVERITIES = [
    s for s in SyslogSeverity if s.value <= TIMELINE_SYSLOG_MAX_SEVERITY.value
]


def _iso(dt: datetime.datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def build_device_timeline(db: Session, device_id: uuid.UUID, hours: int = 72, limit: int = 200) -> list[dict]:
    """Merge alert lifecycle events, drift detections, notable syslog
    lines, and config deployments for `device_id` over the last `hours`
    into one list of timeline entries, newest first.
    """
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    events: list[dict] = []

    alerts = (
        db.query(Alert)
        .filter(Alert.device_id == device_id, Alert.created_at >= since)
        .order_by(desc(Alert.created_at))
        .all()
    )
    for a in alerts:
        severity = a.severity.value if hasattr(a.severity, "value") else a.severity
        source = a.source.value if hasattr(a.source, "value") else a.source
        events.append(
            {
                "kind": "alert_raised",
                "occurred_at": _iso(a.created_at),
                "severity": severity,
                "title": a.category,
                "detail": a.message,
                "ref_id": str(a.id),
                "meta": {"source": source, "suppressed": bool(a.suppressed)},
            }
        )
        if a.resolved and a.resolved_at:
            events.append(
                {
                    "kind": "alert_resolved",
                    "occurred_at": _iso(a.resolved_at),
                    "severity": "info",
                    "title": f"Resolved: {a.category}",
                    "detail": f"Resolved by {a.resolved_by}" if a.resolved_by else "Resolved",
                    "ref_id": str(a.id),
                    "meta": {"source": source},
                }
            )

    drifts = (
        db.query(ConfigDrift)
        .filter(ConfigDrift.device_id == device_id, ConfigDrift.detected_at >= since)
        .order_by(desc(ConfigDrift.detected_at))
        .all()
    )
    for d in drifts:
        severity = d.severity.value if hasattr(d.severity, "value") else d.severity
        events.append(
            {
                "kind": "config_drift",
                "occurred_at": _iso(d.detected_at),
                "severity": severity,
                "title": f"Config drift detected ({severity})",
                "detail": d.ai_summary or f"+{d.added_lines}/-{d.removed_lines}/~{d.modified_lines} lines, risk {d.risk_score}",
                "ref_id": str(d.id),
                "meta": {"risk_score": d.risk_score, "compliance_score": d.compliance_score},
            }
        )

    syslogs = (
        db.query(SyslogMessage)
        .filter(
            SyslogMessage.device_id == device_id,
            SyslogMessage.received_at >= since,
            SyslogMessage.severity.in_(_NOTABLE_SYSLOG_SEVERITIES),
        )
        .order_by(desc(SyslogMessage.received_at))
        .limit(limit)
        .all()
    )
    for row in syslogs:
        severity_label = row.severity.name.lower() if row.severity is not None else "unknown"
        is_critical = row.severity is not None and row.severity.value <= SyslogSeverity.CRITICAL.value
        events.append(
            {
                "kind": "syslog",
                "occurred_at": _iso(row.received_at),
                "severity": "critical" if is_critical else "warning",
                "title": f"Syslog ({severity_label})",
                "detail": row.message,
                "ref_id": str(row.id),
                "meta": {"facility": row.facility, "tag": row.tag},
            }
        )

    deployments = (
        db.query(Deployment)
        .filter(Deployment.device_id == device_id, Deployment.created_at >= since)
        .order_by(desc(Deployment.created_at))
        .all()
    )
    for dep in deployments:
        status = dep.status.value if hasattr(dep.status, "value") else dep.status
        ts = dep.completed_at or dep.started_at or dep.created_at
        events.append(
            {
                "kind": "deployment",
                "occurred_at": _iso(ts),
                "severity": "critical" if status == "failed" else "info",
                "title": f"Config change deployed ({status})",
                "detail": dep.error_message or f"via {dep.protocol}",
                "ref_id": str(dep.id),
                "meta": {"change_request_id": str(dep.change_request_id), "status": status},
            }
        )

    events = [e for e in events if e["occurred_at"] is not None]
    events.sort(key=lambda e: e["occurred_at"], reverse=True)
    return events[:limit]


def build_device_overview(db: Session, device: Device, hours: int = 72) -> dict:
    """Everything the unified device panel needs in one call: identity,
    live health, recent counts per source, and the merged timeline.
    """
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)

    health = metrics_service.device_health(db, device)
    timeline = build_device_timeline(db, device.id, hours=hours)

    active_alert_count = (
        db.query(Alert)
        .filter(Alert.device_id == device.id, Alert.resolved.is_(False))
        .count()
    )
    drift_count = (
        db.query(ConfigDrift)
        .filter(ConfigDrift.device_id == device.id, ConfigDrift.detected_at >= since)
        .count()
    )
    notable_syslog_count = (
        db.query(SyslogMessage)
        .filter(
            SyslogMessage.device_id == device.id,
            SyslogMessage.received_at >= since,
            SyslogMessage.severity.in_(_NOTABLE_SYSLOG_SEVERITIES),
        )
        .count()
    )
    deployment_count = (
        db.query(Deployment)
        .filter(Deployment.device_id == device.id, Deployment.created_at >= since)
        .count()
    )

    return {
        "device_id": str(device.id),
        "hostname": device.hostname,
        "ip_address": device.ip_address,
        "vendor": device.vendor.value if hasattr(device.vendor, "value") else device.vendor,
        "status": device.status.value if hasattr(device.status, "value") else device.status,
        "window_hours": hours,
        "health": health,
        "active_alert_count": active_alert_count,
        "drift_count": drift_count,
        "notable_syslog_count": notable_syslog_count,
        "deployment_count": deployment_count,
        "timeline": timeline,
    }

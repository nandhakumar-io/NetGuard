"""War-room mode -- one click from a critical alert (or an open Incident)
to a single assembled view: affected devices, active change requests
touching them, recent config changes, and a ready-to-post Slack/Teams
summary -- instead of an engineer hunting across the Alert Center,
Change Requests, Config Drift, and Slack tabs mid-outage.

Deliberately read-only and assembled fresh on every call (nothing is
persisted as its own "war room" entity) -- the underlying alert group,
change requests, and config history all keep changing during a live
incident, and a stale cached snapshot would be actively misleading for
the one use case (an active outage) where staleness matters most.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.alert import Alert
from app.models.audit_log import AuditLog
from app.models.change_request import ChangeRequest, ChangeStatus
from app.models.device import Device
from app.models.incident import Incident
from app.services import incident_service, notification_service

# CRs in any of these states are still "in flight" and relevant to an
# active incident -- a merely DRAFT or long-since-resolved CR isn't.
ACTIVE_CR_STATUSES = (
    ChangeStatus.PENDING_APPROVAL,
    ChangeStatus.APPROVED,
    ChangeStatus.VALIDATING,
    ChangeStatus.DEPLOYING,
    ChangeStatus.MONITORING,
)

RECENT_CONFIG_CHANGE_LOOKBACK_HOURS = 24


def _affected_device_ids(db: Session, alerts: list[Alert]) -> list[uuid.UUID]:
    return sorted({a.device_id for a in alerts if a.device_id}, key=str)


def _active_change_requests(db: Session, device_ids: list[uuid.UUID]) -> list[ChangeRequest]:
    if not device_ids:
        return []
    # additional_device_ids is a JSON-encoded text column (see
    # ChangeRequest docstring) rather than a join table, so multi-device
    # CRs can't be filtered in SQL -- pull the active-status set and
    # filter in Python against both device_id and the JSON list.
    candidates = (
        db.query(ChangeRequest)
        .filter(ChangeRequest.status.in_(ACTIVE_CR_STATUSES))
        .order_by(ChangeRequest.created_at.desc())
        .all()
    )
    device_id_strs = {str(d) for d in device_ids}
    matched = []
    for cr in candidates:
        ids = {str(cr.device_id)}
        if cr.additional_device_ids:
            try:
                ids |= set(json.loads(cr.additional_device_ids))
            except (json.JSONDecodeError, TypeError):
                pass
        if ids & device_id_strs:
            matched.append(cr)
    return matched


def _recent_config_changes(db: Session, hostnames: list[str], *, hours: int) -> list[AuditLog]:
    if not hostnames:
        return []
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    return (
        db.query(AuditLog)
        .filter(AuditLog.device_hostname.in_(hostnames))
        .filter(AuditLog.created_at >= since)
        .order_by(AuditLog.created_at.desc())
        .limit(100)
        .all()
    )


def _serialize_alert(a: Alert) -> dict:
    return {
        "id": str(a.id),
        "severity": a.severity.value if hasattr(a.severity, "value") else a.severity,
        "category": a.category,
        "message": a.message,
        "device_id": str(a.device_id) if a.device_id else None,
        "acknowledged": a.acknowledged,
        "resolved": a.resolved,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _serialize_cr(cr: ChangeRequest) -> dict:
    return {
        "id": str(cr.id),
        "status": cr.status.value if hasattr(cr.status, "value") else cr.status,
        "priority": cr.priority.value if hasattr(cr.priority, "value") else cr.priority,
        "description": cr.description,
        "device_id": str(cr.device_id),
        "created_at": cr.created_at.isoformat() if cr.created_at else None,
    }


def _serialize_config_change(row: AuditLog) -> dict:
    return {
        "id": str(row.id),
        "actor": row.actor,
        "action": row.action,
        "device_hostname": row.device_hostname,
        "result": row.result,
        "detail": row.detail,
        "change_request_id": str(row.change_request_id) if row.change_request_id else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def assemble_war_room(
    db: Session,
    *,
    alert_id: uuid.UUID | None = None,
    incident_id: uuid.UUID | None = None,
    config_change_lookback_hours: int = RECENT_CONFIG_CHANGE_LOOKBACK_HOURS,
) -> dict:
    """Builds the full war-room payload starting from either a single
    alert (typically the one just clicked from the Alert Center) or an
    already-open Incident. Exactly one of alert_id/incident_id must be
    given.

    Returns:
      {
        anchor: {type: "alert"|"incident", id},
        alert_group: [alerts in the correlated group],
        affected_devices: [device summaries],
        active_change_requests: [...],
        recent_config_changes: [...],  # last `config_change_lookback_hours`
        slack_summary: str,            # ready to post as-is
      }
    """
    if bool(alert_id) == bool(incident_id):
        raise ValueError("Exactly one of alert_id or incident_id is required")

    if incident_id:
        incident = db.get(Incident, incident_id)
        if not incident:
            raise LookupError(f"Incident {incident_id} not found")
        group_ids = incident_service.alert_ids_list(incident)
        alerts = db.query(Alert).filter(Alert.id.in_(group_ids)).all() if group_ids else []
        anchor = {"type": "incident", "id": str(incident.id), "title": incident.title}
    else:
        root_alert = db.get(Alert, alert_id)
        if not root_alert:
            raise LookupError(f"Alert {alert_id} not found")
        root_id = root_alert.root_cause_alert_id or root_alert.id
        group_ids = incident_service.alert_group_ids(db, root_id)
        alerts = db.query(Alert).filter(Alert.id.in_(group_ids)).all() if group_ids else [root_alert]
        anchor = {"type": "alert", "id": str(root_alert.id), "category": root_alert.category}

    device_ids = _affected_device_ids(db, alerts)
    devices = db.query(Device).filter(Device.id.in_(device_ids)).all() if device_ids else []
    hostnames = [d.hostname for d in devices]

    active_crs = _active_change_requests(db, device_ids)
    config_changes = _recent_config_changes(db, hostnames, hours=config_change_lookback_hours)

    summary = _build_slack_summary(
        anchor=anchor, alerts=alerts, devices=devices, active_crs=active_crs, config_changes=config_changes
    )

    return {
        "anchor": anchor,
        "alert_group": [_serialize_alert(a) for a in alerts],
        "affected_devices": [
            {"id": str(d.id), "hostname": d.hostname, "site": d.site, "vendor": getattr(d, "vendor", None)}
            for d in devices
        ],
        "active_change_requests": [_serialize_cr(cr) for cr in active_crs],
        "recent_config_changes": [_serialize_config_change(r) for r in config_changes],
        "slack_summary": summary,
    }


def _build_slack_summary(*, anchor: dict, alerts: list[Alert], devices: list[Device], active_crs: list[ChangeRequest], config_changes: list[AuditLog]) -> str:
    unresolved = [a for a in alerts if not a.resolved]
    critical_count = sum(1 for a in alerts if str(getattr(a.severity, "value", a.severity)) == "critical")

    lines = [":rotating_light: *War room assembled*"]
    if anchor["type"] == "incident":
        lines.append(f"Incident: {anchor['title']} (`{anchor['id']}`)")
    else:
        lines.append(f"Root cause: {anchor.get('category', 'unknown')} (`{anchor['id']}`)")

    lines.append(
        f"Alerts: {len(alerts)} total, {len(unresolved)} still active, {critical_count} critical"
    )
    if devices:
        hostnames = ", ".join(d.hostname for d in devices[:10])
        more = f" (+{len(devices) - 10} more)" if len(devices) > 10 else ""
        lines.append(f"Affected devices: {hostnames}{more}")
    if active_crs:
        lines.append(f"In-flight change requests: {len(active_crs)}")
        for cr in active_crs[:5]:
            status = cr.status.value if hasattr(cr.status, "value") else cr.status
            lines.append(f"  • `{str(cr.id)[:8]}` [{status}] {cr.description}")
    if config_changes:
        lines.append(f"Config activity in the last {RECENT_CONFIG_CHANGE_LOOKBACK_HOURS}h: {len(config_changes)} event(s)")

    return "\n".join(lines)


def post_war_room_summary(db: Session, war_room: dict) -> None:
    """Posts the assembled slack_summary to Slack/Teams via the existing
    notification fan-out, with a deep link back to the anchor. Separate
    from assemble_war_room() so the UI can render the assembled view
    without side effects, and only post when the user explicitly clicks
    "post to war room".
    """
    anchor = war_room["anchor"]
    if anchor["type"] == "incident":
        link = f"{settings.FRONTEND_URL.rstrip('/')}/incidents/{anchor['id']}"
    else:
        link = f"{settings.FRONTEND_URL.rstrip('/')}/alerts/{anchor['id']}"

    message = f"{war_room['slack_summary']}\n<{link}|Open in NetGuard>"
    notification_service.notify(event="War Room Assembled", message=message, severity="critical")

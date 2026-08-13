"""Two-way ChatOps: lets an on-call engineer approve/reject a change
request, trigger a rollback, or query device status from Slack or Teams
instead of context-switching to the NetGuard UI.

Outbound notifications (Slack/Teams incoming webhooks) already exist in
app.services.notification_service -- this module is the *inbound* half:
verifying a request really came from Slack/Teams, resolving which
NetGuard user sent it (via the slack_user_id/msteams_user_id link set up
through POST /chatops/links), parsing a short command grammar, and
executing it by calling straight into the same endpoint functions the UI
calls (app.api.change_requests / app.api.deployments) so ChatOps can
never drift from what the "Approve" button in the UI actually does --
same RBAC checks, same dual-approval/approval-chain/maintenance-window
rules, same audit trail.

Command grammar (case-insensitive, leading bot mention/slash-command name
already stripped by the caller):

    approve <change-request-id>
    reject <change-request-id>
    rollback <deployment-id>
    rollback confirm <deployment-id>
    status <hostname>
    ack <alert-id>
    resolve <alert-id>
    alerts [hostname]
    fleet
    drift [hostname]
    backup <hostname>
    whois <hostname>
    help
"""
import hashlib
import hmac
import logging
import time
import uuid
from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core import vm_client
from app.core.config import settings
from app.models.alert import Alert
from app.models.config_drift import ConfigDrift, DriftStatus
from app.models.device import Device
from app.models.user import User, UserRole
from app.services import alert_service, drift_service, metrics_service

logger = logging.getLogger(__name__)

SLACK_TIMESTAMP_TOLERANCE_SECONDS = 60 * 5


@dataclass
class ChatOpsResult:
    text: str
    ok: bool = True
    # Optional structured payload alongside `text` -- e.g. a list of
    # {alert_id, hostname, severity} dicts for `alerts`, so the Slack
    # layer (app.api.chatops) can render per-item action buttons without
    # re-parsing the text. Unused by Teams, which is text-only.
    items: list[dict] = field(default_factory=list)
    # Free-form category hint for the Slack layer's color-coding
    # ("critical" | "warning" | "info" | None).
    severity: str | None = None


# --- Inbound request verification -----------------------------------------


def verify_slack_signature(body: bytes, timestamp: str | None, signature: str | None) -> bool:
    """Slack's request-signing scheme: HMAC-SHA256("v0:{ts}:{body}",
    SLACK_SIGNING_SECRET), compared to the `v0=...` X-Slack-Signature
    header. Also rejects requests with a stale timestamp (replay
    protection), same as Slack's own reference implementation.

    Returns False (never raises) on any missing config/header -- callers
    turn that into a 401, same "fail closed" posture as an unset
    signature checker anywhere else in this codebase.
    """
    if not settings.SLACK_SIGNING_SECRET or not timestamp or not signature:
        return False
    try:
        if abs(time.time() - float(timestamp)) > SLACK_TIMESTAMP_TOLERANCE_SECONDS:
            return False
    except ValueError:
        return False

    base = f"v0:{timestamp}:{body.decode('utf-8', errors='replace')}"
    computed = "v0=" + hmac.new(
        settings.SLACK_SIGNING_SECRET.encode(), base.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed, signature)


def verify_teams_hmac(body: bytes, auth_header: str | None) -> bool:
    """Microsoft Teams "Outgoing Webhook" HMAC verification: the
    connector's security token is used as the HMAC-SHA256 key over the
    raw request body, base64-encoded, and sent as `Authorization: HMAC
    <signature>`. See Teams' outgoing-webhook documentation."""
    import base64

    if not settings.TEAMS_OUTGOING_WEBHOOK_SECRET or not auth_header:
        return False
    if not auth_header.startswith("HMAC "):
        return False
    provided = auth_header[len("HMAC "):].strip()
    try:
        key = base64.b64decode(settings.TEAMS_OUTGOING_WEBHOOK_SECRET)
    except Exception:
        key = settings.TEAMS_OUTGOING_WEBHOOK_SECRET.encode()
    computed = base64.b64encode(hmac.new(key, body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(computed, provided)


# --- User resolution --------------------------------------------------------


def resolve_user(db: Session, *, slack_user_id: str | None = None, msteams_user_id: str | None = None) -> User | None:
    query = db.query(User)
    if slack_user_id:
        return query.filter(User.slack_user_id == slack_user_id).first()
    if msteams_user_id:
        return query.filter(User.msteams_user_id == msteams_user_id).first()
    return None


# --- Command parsing + execution -------------------------------------------


def _parse_uuid(token: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(token)
    except ValueError:
        raise ChatOpsCommandError(
            f"'{token}' doesn't look like a valid {label} ID. Copy it from the NetGuard UI "
            "(it's a UUID, e.g. 3fa85f64-5717-4562-b3fc-2c963f66afa6)."
        )


class ChatOpsCommandError(Exception):
    pass


HELP_TEXT = (
    "*NetGuard ChatOps commands*\n"
    "\n"
    "*Triage*\n"
    "`status <hostname>` -- quick health/alert/drift snapshot for a device\n"
    "`alerts [hostname]` -- active alert summary, fleet-wide or per-device\n"
    "`fleet` -- fleet health overview (device counts by color + avg score)\n"
    "`drift [hostname]` -- fleet drift posture, or per-device drift status\n"
    "`whois <hostname>` -- deep device overview (health + recent timeline)\n"
    "\n"
    "*Actions*\n"
    "`approve <change-request-id>` -- approve a pending change request\n"
    "`reject <change-request-id>` -- reject a pending change request\n"
    "`rollback <deployment-id>` -- preview a rollback's diff (dry run, nothing pushed)\n"
    "`rollback confirm <deployment-id>` -- actually queue the previewed rollback\n"
    "`ack <alert-id>` -- acknowledge an alert\n"
    "`resolve <alert-id>` -- resolve an alert\n"
    "`backup <hostname>` -- trigger an on-demand config backup\n"
    "\n"
    "*Info*\n"
    "`help` -- show this message"
)


def execute_command(db: Session, user: User, raw_text: str) -> ChatOpsResult:
    """Parses and runs one ChatOps command as `user`. Never raises --
    every failure path (bad syntax, not found, RBAC denial, business-rule
    conflict) is converted into a plain-text reply, since the caller is a
    chat message, not an API client that can handle a stack trace."""
    text = (raw_text or "").strip()
    if not text or text.lower() in ("help", "?"):
        return ChatOpsResult(text=HELP_TEXT)

    parts = text.split(maxsplit=1)
    action = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    try:
        if action == "approve":
            return _approve(db, user, arg)
        if action == "reject":
            return _reject(db, user, arg)
        if action == "rollback":
            return _rollback(db, user, arg)
        if action == "status":
            return _status(db, arg)
        if action == "ack":
            return _ack(db, user, arg)
        if action == "resolve":
            return _resolve(db, user, arg)
        if action == "alerts":
            return _alerts(db, arg)
        if action == "fleet":
            return _fleet(db)
        if action == "drift":
            return _drift(db, arg)
        if action == "backup":
            return _backup(db, user, arg)
        if action == "whois":
            return _whois(db, arg)
        return ChatOpsResult(
            ok=False,
            text=f"Unknown command '{action}'. Try `help` for the list of commands.",
        )
    except ChatOpsCommandError as exc:
        return ChatOpsResult(ok=False, text=str(exc))
    except HTTPException as exc:
        return ChatOpsResult(ok=False, text=f"Couldn't do that: {exc.detail}")
    except Exception:
        logger.exception("Unhandled error executing ChatOps command %r", text)
        return ChatOpsResult(ok=False, text="Something went wrong running that command. Check the NetGuard UI.")


def _approve(db: Session, user: User, arg: str) -> ChatOpsResult:
    from app.api.change_requests import (
        approve_change_request,  # local import: avoids a circular import at module load
    )

    if not arg:
        raise ChatOpsCommandError("Usage: `approve <change-request-id>`")
    cr_id = _parse_uuid(arg, "change request")
    result = approve_change_request(cr_id, db=db, current_user=user)
    return ChatOpsResult(text=f"✅ Change request `{cr_id}` approved by {user.full_name}. Status: *{result.status.value}*.")


def _reject(db: Session, user: User, arg: str) -> ChatOpsResult:
    from app.api.change_requests import reject_change_request

    if not arg:
        raise ChatOpsCommandError("Usage: `reject <change-request-id>`")
    cr_id = _parse_uuid(arg, "change request")
    result = reject_change_request(cr_id, db=db, current_user=user)
    return ChatOpsResult(text=f"🚫 Change request `{cr_id}` rejected by {user.full_name}. Status: *{result.status.value}*.")


def _rollback(db: Session, user: User, arg: str) -> ChatOpsResult:
    """Two-step: `rollback <deployment-id>` shows a dry-run diff of what
    the rollback would change (no ChangeRequest created, nothing pushed);
    `rollback confirm <deployment-id>` actually queues it. Mirrors the
    Deployments page, which shows the same preview before its "Roll Back"
    button is confirmed -- ChatOps shouldn't be able to push a config
    change to a device with less scrutiny than the UI gives it.
    """
    from app.api.deployments import preview_deployment_rollback, rollback_deployment

    if not arg:
        raise ChatOpsCommandError("Usage: `rollback <deployment-id>` to preview, then `rollback confirm <deployment-id>` to execute.")

    parts = arg.split(maxsplit=1)
    if parts[0].lower() == "confirm":
        if len(parts) < 2:
            raise ChatOpsCommandError("Usage: `rollback confirm <deployment-id>`")
        deployment_id = _parse_uuid(parts[1].strip(), "deployment")
        result = rollback_deployment(deployment_id, db=db, current_user=user)
        return ChatOpsResult(text=f"⏪ {result['message']} (new change request `{result['change_request_id']}`).")

    deployment_id = _parse_uuid(arg, "deployment")
    preview = preview_deployment_rollback(deployment_id, db=db, _current_user=user)

    if preview.blocked:
        return ChatOpsResult(
            ok=False,
            severity="warning",
            text=f":no_entry: Can't roll back `{deployment_id}` yet -- {preview.blocked_reason}",
        )
    if preview.identical:
        return ChatOpsResult(
            text=f":information_source: `{preview.hostname}` already matches pre-deploy snapshot v{preview.target_version} -- nothing to roll back.",
        )

    warning_line = f"\n:warning: {preview.warning}" if preview.warning else ""
    text = (
        f"*Rollback preview for* `{preview.hostname}` *(deployment `{deployment_id}`)*\n"
        f"Restoring pre-deploy snapshot v{preview.target_version}: "
        f"`+{preview.added_lines}/-{preview.removed_lines}` lines.{warning_line}\n"
        f"Nothing has been changed yet -- run `rollback confirm {deployment_id}` to apply, "
        "or click Confirm below."
    )
    return ChatOpsResult(text=text, items=[{"rollback_deployment_id": str(deployment_id)}])


def _get_device_or_error(db: Session, hostname: str) -> Device:
    device = db.query(Device).filter(Device.hostname == hostname).first()
    if not device:
        raise ChatOpsCommandError(f"No device found with hostname `{hostname}`.")
    return device


def _status(db: Session, arg: str) -> ChatOpsResult:
    """Quick triage snapshot: online/offline status plus health color,
    active alert count, open drift count, and uptime -- everything an
    on-call engineer needs to decide whether a device needs attention
    without leaving chat.
    """
    if not arg:
        raise ChatOpsCommandError("Usage: `status <hostname>`")
    device = db.query(Device).filter(Device.hostname == arg).first()
    if not device:
        return ChatOpsResult(ok=False, text=f"No device found with hostname `{arg}`.")

    latest_metric = vm_client.latest_device_metrics(device.id)
    status_value = device.status.value if hasattr(device.status, "value") else device.status
    health_color = (latest_metric or {}).get("health_color") or "unknown"

    active_alerts = (
        db.query(Alert).filter(Alert.device_id == device.id, Alert.resolved.is_(False)).count()
    )
    open_drifts = (
        db.query(ConfigDrift)
        .filter(ConfigDrift.device_id == device.id, ConfigDrift.status == DriftStatus.OPEN)
        .count()
    )

    uptime_seconds = (latest_metric or {}).get("uptime_seconds")
    uptime_text = _format_uptime(uptime_seconds) if uptime_seconds is not None else "unknown"

    lines = [
        f"*{device.hostname}* ({device.ip_address})",
        f"Status: *{status_value}*  |  Health: *{health_color}*",
        f"Active alerts: *{active_alerts}*  |  Open drifts: *{open_drifts}*",
        f"Uptime: {uptime_text}",
    ]
    if device.last_snmp_poll_at:
        lines.append(f"Last SNMP poll: {device.last_snmp_poll_at.isoformat()}")

    severity = "critical" if active_alerts and health_color == "red" else (
        "warning" if active_alerts or open_drifts or health_color in ("red", "yellow") else "info"
    )
    return ChatOpsResult(text="\n".join(lines), severity=severity)


def _format_uptime(seconds: int) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _ack(db: Session, user: User, arg: str) -> ChatOpsResult:
    if not arg:
        raise ChatOpsCommandError("Usage: `ack <alert-id>`")
    alert_id = _parse_uuid(arg, "alert")
    try:
        alert = alert_service.acknowledge_alert(db, alert_id, user.email)
    except ValueError as exc:
        raise ChatOpsCommandError(str(exc))
    return ChatOpsResult(text=f"✅ Alert `{alert.id}` (*{alert.category}*) acknowledged by {user.full_name}.")


def _resolve(db: Session, user: User, arg: str) -> ChatOpsResult:
    if not arg:
        raise ChatOpsCommandError("Usage: `resolve <alert-id>`")
    alert_id = _parse_uuid(arg, "alert")
    try:
        alert = alert_service.resolve_alert(db, alert_id, user.email)
    except ValueError as exc:
        raise ChatOpsCommandError(str(exc))
    return ChatOpsResult(text=f"🟢 Alert `{alert.id}` (*{alert.category}*) resolved by {user.full_name}.")


def _alerts(db: Session, arg: str) -> ChatOpsResult:
    hostname = arg.strip()
    if not hostname:
        summary = alert_service.get_alert_summary(db)
        lines = [
            "*Fleet active alerts*",
            f":red_circle: Critical: *{summary['critical']}*",
            f":large_orange_circle: Warning: *{summary['warning']}*",
            f":large_blue_circle: Info: *{summary['info']}*",
            f"Total active: *{summary['active_total']}*",
        ]
        severity = "critical" if summary["critical"] else ("warning" if summary["warning"] else "info")
        return ChatOpsResult(text="\n".join(lines), severity=severity)

    device = _get_device_or_error(db, hostname)
    from sqlalchemy import case

    severity_order = case(
        (Alert.severity == "critical", 1),
        (Alert.severity == "warning", 2),
        (Alert.severity == "info", 3),
        else_=4
    )

    alerts = (
        db.query(Alert)
        .filter(Alert.device_id == device.id, Alert.resolved.is_(False))
        .order_by(severity_order, Alert.last_seen_at.desc())
        .limit(20)
        .all()
    )
    if not alerts:
        return ChatOpsResult(text=f"*{device.hostname}* has no active alerts. :white_check_mark:", severity="info")

    lines = [f"*Active alerts for {device.hostname}* ({len(alerts)})"]
    items = []
    worst_severity = "info"
    for a in alerts:
        sev = a.severity.value if hasattr(a.severity, "value") else a.severity
        if sev == "critical":
            worst_severity = "critical"
        elif sev == "warning" and worst_severity != "critical":
            worst_severity = "warning"
        ack_flag = " (ack'd)" if a.acknowledged else ""
        lines.append(f"`{a.id}` *[{sev}]* {a.category}{ack_flag} -- {a.message}")
        items.append({"alert_id": str(a.id), "hostname": device.hostname, "severity": sev, "category": a.category})
    return ChatOpsResult(text="\n".join(lines), items=items, severity=worst_severity)


def _fleet(db: Session) -> ChatOpsResult:
    summary = metrics_service.fleet_health_summary(db)
    lines = [
        "*Fleet health overview*",
        f":large_green_circle: Green: *{summary['green']}*  "
        f":large_yellow_circle: Yellow: *{summary['yellow']}*  "
        f":red_circle: Red: *{summary['red']}*  "
        f"Unknown: *{summary['unknown']}*",
        f"Devices monitored: *{summary['devices_monitored']}*",
    ]
    if summary["average_health_score"] is not None:
        lines.append(f"Average health score: *{summary['average_health_score']}*")
    if summary["devices_with_stale_metrics"]:
        lines.append(f":warning: Devices with stale metrics: *{summary['devices_with_stale_metrics']}*")
    severity = "critical" if summary["red"] else ("warning" if summary["yellow"] else "info")
    return ChatOpsResult(text="\n".join(lines), severity=severity)


def _drift(db: Session, arg: str) -> ChatOpsResult:
    hostname = arg.strip()
    if not hostname:
        summary = drift_service.fleet_summary(db)
        by_sev = summary["by_severity"]
        lines = [
            "*Fleet drift posture*",
            f"Open drifts: *{summary['total_open_drifts']}*  |  Devices drifted: *{summary['devices_drifted']}*",
            f"Average compliance score: *{summary['average_compliance_score']}*",
            f"By severity -- critical: *{by_sev.get('critical', 0)}*, high: *{by_sev.get('high', 0)}*, "
            f"medium: *{by_sev.get('medium', 0)}*, low: *{by_sev.get('low', 0)}*",
        ]
        if summary["rollback_recommended_count"]:
            lines.append(f":rotating_light: Rollback recommended on *{summary['rollback_recommended_count']}* device(s)")
        severity = "critical" if by_sev.get("critical") else ("warning" if summary["total_open_drifts"] else "info")
        return ChatOpsResult(text="\n".join(lines), severity=severity)

    device = _get_device_or_error(db, hostname)
    drifts = drift_service.list_drifts(db, device_id=device.id, status=DriftStatus.OPEN, limit=20)
    if not drifts:
        return ChatOpsResult(text=f"*{device.hostname}* has no open config drift. :white_check_mark:", severity="info")

    lines = [f"*Open drift for {device.hostname}* ({len(drifts)})"]
    worst_severity = "info"
    for d in drifts:
        sev = d.severity.value if hasattr(d.severity, "value") else d.severity
        if sev in ("critical", "high"):
            worst_severity = "critical" if sev == "critical" else ("critical" if worst_severity == "critical" else "warning")
        lines.append(
            f"`{d.id}` *[{sev}]* compliance {d.compliance_score} "
            f"(+{d.added_lines}/-{d.removed_lines}/~{d.modified_lines})"
        )
    return ChatOpsResult(text="\n".join(lines), severity=worst_severity if worst_severity != "info" else "warning")


def _backup(db: Session, user: User, arg: str) -> ChatOpsResult:
    from app.api.config_management import (
        backup_config,  # local import: avoids a circular import at module load
    )

    if not arg:
        raise ChatOpsCommandError("Usage: `backup <hostname>`")
    # backup_config is normally gated by Depends(CONFIG_WRITE_ROLES); calling
    # it directly (bypassing FastAPI's dependency injection) skips that
    # check, so it's re-applied here explicitly -- same rule, same roles.
    if user.role != UserRole.NETWORK_ADMIN:
        raise ChatOpsCommandError("Only Network Administrators can trigger a config backup.")
    device = _get_device_or_error(db, arg)
    result = backup_config(device.id, payload=None, db=db, current_user=user)
    return ChatOpsResult(text=f"💾 {result.message}")


def _whois(db: Session, arg: str) -> ChatOpsResult:
    from app.services import device_overview_service

    if not arg:
        raise ChatOpsCommandError("Usage: `whois <hostname>`")
    device = _get_device_or_error(db, arg)
    overview = device_overview_service.build_device_overview(db, device)
    health = overview["health"]

    lines = [
        f"*{overview['hostname']}* ({device.ip_address}) -- {overview['vendor']}",
        f"Status: *{overview['status']}*  |  Health: *{health['health_color']}* "
        f"({health['health_score'] if health['health_score'] is not None else 'n/a'})",
        f"Active alerts: *{overview['active_alert_count']}*  |  "
        f"Drift events ({overview['window_hours']}h): *{overview['drift_count']}*",
        f"Notable syslog ({overview['window_hours']}h): *{overview['notable_syslog_count']}*  |  "
        f"Deployments ({overview['window_hours']}h): *{overview['deployment_count']}*",
    ]
    recent = overview["timeline"][:5]
    if recent:
        lines.append("Recent activity:")
        for event in recent:
            title = event.get("title") or event.get("kind") or "event"
            occurred_at = event.get("occurred_at")
            lines.append(f"  • {occurred_at}: {title}" if occurred_at else f"  • {title}")

    severity = "critical" if overview["active_alert_count"] and health["health_color"] == "red" else (
        "warning" if overview["active_alert_count"] or overview["drift_count"] else "info"
    )
    return ChatOpsResult(text="\n".join(lines), severity=severity)

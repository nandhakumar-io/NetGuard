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
    status <hostname>
    help
"""
import hashlib
import hmac
import logging
import time
import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.device import Device
from app.models.device_metric import DeviceMetric
from app.models.user import User

logger = logging.getLogger(__name__)

SLACK_TIMESTAMP_TOLERANCE_SECONDS = 60 * 5


@dataclass
class ChatOpsResult:
    text: str
    ok: bool = True


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
    "`approve <change-request-id>` -- approve a pending change request\n"
    "`reject <change-request-id>` -- reject a pending change request\n"
    "`rollback <deployment-id>` -- roll back a failed deployment\n"
    "`status <hostname>` -- show a device's current status\n"
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
    from app.api.deployments import rollback_deployment

    if not arg:
        raise ChatOpsCommandError("Usage: `rollback <deployment-id>`")
    deployment_id = _parse_uuid(arg, "deployment")
    result = rollback_deployment(deployment_id, db=db, current_user=user)
    return ChatOpsResult(text=f"⏪ {result['message']} (new change request `{result['change_request_id']}`).")


def _status(db: Session, arg: str) -> ChatOpsResult:
    if not arg:
        raise ChatOpsCommandError("Usage: `status <hostname>`")
    device = db.query(Device).filter(Device.hostname == arg).first()
    if not device:
        return ChatOpsResult(ok=False, text=f"No device found with hostname `{arg}`.")

    latest_metric = (
        db.query(DeviceMetric)
        .filter(DeviceMetric.device_id == device.id)
        .order_by(DeviceMetric.polled_at.desc())
        .first()
    )
    status_value = device.status.value if hasattr(device.status, "value") else device.status
    lines = [
        f"*{device.hostname}* ({device.ip_address})",
        f"Status: *{status_value}*",
    ]
    if latest_metric:
        health = latest_metric.health_color.value if hasattr(latest_metric.health_color, "value") else latest_metric.health_color
        lines.append(f"Health: *{health}* (polled {latest_metric.polled_at.isoformat() if latest_metric.polled_at else 'unknown'})")
    if device.last_snmp_poll_at:
        lines.append(f"Last SNMP poll: {device.last_snmp_poll_at.isoformat()}")
    return ChatOpsResult(text="\n".join(lines))

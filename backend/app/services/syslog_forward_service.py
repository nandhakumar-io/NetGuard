"""Outbound remote syslog forwarding.

NetGuard has always been a syslog *receiver* (app.services.syslog_service
listens for UDP syslog from managed devices). This module is the other
direction: NetGuard as a *sender*, relaying its own notify() events (the
same alerts/deployments/drift/etc. that already fan out to Slack/Teams/
email/webhooks -- see app.services.notification_service.notify) to one or
more externally-configured syslog collectors, e.g. Splunk, Graylog, an
rsyslog relay, or a SIEM ingest point.

Framed as either legacy RFC 3164 (`<PRI>timestamp host tag: msg`, the
lowest-common-denominator format most appliances still expect) or RFC
5424 (`<PRI>1 timestamp host app - - - msg`), selectable per destination.

Delivery is always best-effort: a forwarding failure (unreachable
collector, DNS failure, refused TCP connection) must never block or
raise into the caller, mirroring every other channel in
notification_service. Failures are recorded on the SyslogDestination row
(last_error/last_error_at) so they're visible in the UI instead of only
in server logs.
"""
from __future__ import annotations

import datetime
import logging
import socket

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.syslog_destination import SyslogDestination

logger = logging.getLogger(__name__)

# Map NetGuard's 3-tier severity scale to syslog severity numbers
# (RFC 5424 section 6.2.1). notify() only ever uses info/warning/critical,
# so anything else falls back to "info" (severity 6).
_SEVERITY_MAP = {
    "critical": 2,   # Critical
    "warning": 4,    # Warning
    "info": 6,       # Informational
}
_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}

_HOSTNAME = socket.gethostname()


def _pri(facility: int, severity_name: str) -> int:
    return facility * 8 + _SEVERITY_MAP.get(severity_name, 6)


def _format_message(dest: SyslogDestination, event: str, message: str, severity: str) -> bytes:
    pri = _pri(dest.facility, severity)
    now = datetime.datetime.now(datetime.timezone.utc)
    tag = "netguard"
    single_line = message.replace("\n", " ").strip()
    if dest.use_rfc5424:
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        # <PRI>VERSION TIMESTAMP HOSTNAME APP-NAME PROCID MSGID MSG
        line = f"<{pri}>1 {ts} {_HOSTNAME} {tag} - {event.replace(' ', '_')} - {event}: {single_line}"
    else:
        ts = now.strftime("%b %d %H:%M:%S")
        line = f"<{pri}>{ts} {_HOSTNAME} {tag}: {event}: {single_line}"
    return line.encode("utf-8", errors="replace")


def send_to_destination(dest: SyslogDestination, event: str, message: str, severity: str) -> tuple[bool, str | None]:
    """Sends one syslog datagram/stream to one destination. Never raises;
    returns (success, error) so the caller can persist it."""
    payload = _format_message(dest, event, message, severity)
    try:
        if dest.protocol == "tcp" or getattr(dest.protocol, "value", dest.protocol) == "tcp":
            with socket.create_connection((dest.host, dest.port), timeout=5) as sock:
                # RFC 6587 octet-counted framing works with more strict TCP
                # collectors; a trailing newline is the widely-supported
                # non-transparent-framing fallback most collectors accept.
                sock.sendall(payload + b"\n")
        else:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(5)
                sock.sendto(payload, (dest.host, dest.port))
        return True, None
    except Exception as exc:  # noqa: BLE001 - best-effort, never raise
        logger.warning("Failed to forward syslog to destination %s (%s:%s)", dest.name, dest.host, dest.port, exc_info=True)
        return False, str(exc)


def fan_out(db: Session, event: str, message: str, severity: str = "info") -> None:
    """Forwards one notify() event to every enabled SyslogDestination
    whose min_severity is met. Called from
    notification_service.notify()'s fan-out list; swallows all errors per
    destination so one misconfigured collector can't block delivery to
    the rest or interrupt the caller's workflow.
    """
    if not settings.SYSLOG_FORWARDING_ENABLED:
        return
    destinations = db.query(SyslogDestination).filter(SyslogDestination.enabled.is_(True)).all()
    if not destinations:
        return
    event_rank = _SEVERITY_RANK.get(severity, 0)
    for dest in destinations:
        if _SEVERITY_RANK.get(dest.min_severity, 0) > event_rank:
            continue
        ok, err = send_to_destination(dest, event, message, severity)
        now = datetime.datetime.now(datetime.timezone.utc)
        if ok:
            dest.last_sent_at = now
        else:
            dest.last_error = err
            dest.last_error_at = now
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("Failed to persist syslog forwarding status", exc_info=True)


def send_test_message(db: Session, dest: SyslogDestination) -> tuple[bool, str | None]:
    """Fires one synchronous test message at a destination (for the
    "Send Test" button) and updates its status the same way a real
    fan_out() delivery would."""
    ok, err = send_to_destination(dest, "Test Message", "This is a test message from NetGuard.", "info")
    now = datetime.datetime.now(datetime.timezone.utc)
    if ok:
        dest.last_sent_at = now
        dest.last_error = None
    else:
        dest.last_error = err
        dest.last_error_at = now
    db.commit()
    return ok, err

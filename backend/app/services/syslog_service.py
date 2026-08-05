"""Syslog collection & correlation.

Fills a real data-completeness gap: everything else this app knows about
a device comes from SNMP polling (pollable OIDs -- CPU/mem/temp/fan/
power/interface counters) or config snapshots. Whole classes of events
-- auth failures, hardware fault log lines, ACL deny hits, most
vendor-specific fault text -- are only ever emitted as unsolicited
syslog, never exposed via a poll. A device can be fully green on the
Health Dashboard while its console is logging a slow-burning problem
this app has literally no other way to see.

Two entry points feed the same ingest_message() pipeline:
  * A real asyncio UDP listener (SyslogUDPServer), started from
    app.main's startup hook -- devices point their `logging host`
    config at this app the normal way.
  * POST /syslog/ingest (app.api.syslog) -- lets a TCP-based forwarder
    (rsyslog/syslog-ng relay), a test script, or a device that can only
    do TCP syslog push messages in over HTTP instead.

Parsing supports both wire formats in real use:
  * RFC 3164 ("BSD syslog"): "<PRI>Mmm dd hh:mm:ss HOSTNAME TAG: MSG"
  * RFC 5424: "<PRI>1 TIMESTAMP HOSTNAME APP-NAME PROCID MSGID [SD] MSG"
A message that matches neither shape is still stored (raw + best-effort
PRI extraction), since a malformed/nonstandard line is still evidence
something happened -- silently dropping it would recreate exactly the
blind spot this feature exists to close.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import re

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.device import Device
from app.models.syslog_message import SyslogMessage, SyslogSeverity
from app.services import alert_service, event_bus
from app.models.alert import AlertSeverity, AlertSource  # AlertSource.SYSLOG

logger = logging.getLogger("netguard.syslog")

# --- Parsing -----------------------------------------------------------

_PRI_RE = re.compile(r"^<(\d{1,3})>")
# RFC 5424: "<PRI>1 TIMESTAMP HOST APP-NAME PROCID MSGID [SD or -] MSG"
_RFC5424_RE = re.compile(
    r"^<(?P<pri>\d{1,3})>1\s+(?P<ts>\S+)\s+(?P<host>\S+)\s+(?P<app>\S+)\s+(?P<procid>\S+)\s+"
    r"(?P<msgid>\S+)\s+(?:\[[^\]]*\]|-)\s*(?P<msg>.*)$"
)
# RFC 3164: "<PRI>Mmm dd hh:mm:ss HOST TAG: MSG"  (year is not in the wire
# format at all -- assumed to be "this year", same gap every BSD-syslog
# consumer has).
_RFC3164_RE = re.compile(
    r"^<(?P<pri>\d{1,3})>(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+"
    r"(?P<host>\S+)\s+(?P<tag>[^:\[]+)(?:\[\d+\])?:\s*(?P<msg>.*)$"
)
_RFC3164_MONTHS = {
    m: i + 1
    for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])
}


class ParsedSyslog:
    __slots__ = ("facility", "severity", "hostname", "tag", "message", "device_reported_at")

    def __init__(self, facility, severity, hostname, tag, message, device_reported_at):
        self.facility = facility
        self.severity = severity
        self.hostname = hostname
        self.tag = tag
        self.message = message
        self.device_reported_at = device_reported_at


def _parse_rfc3164_timestamp(ts: str) -> datetime.datetime | None:
    try:
        parts = ts.split()
        month = _RFC3164_MONTHS[parts[0]]
        day = int(parts[1])
        hh, mm, ss = (int(x) for x in parts[2].split(":"))
        now = datetime.datetime.now(datetime.timezone.utc)
        return datetime.datetime(now.year, month, day, hh, mm, ss, tzinfo=datetime.timezone.utc)
    except (KeyError, ValueError, IndexError):
        return None


def parse_syslog_line(raw: str) -> ParsedSyslog:
    """Best-effort parse of one syslog line. Never raises -- an
    unparseable line still comes back with whatever could be salvaged
    (PRI if present, else defaults) and the full text as `message`, so
    ingest_message() always has something to store rather than dropping
    the line outright.
    """
    raw = raw.strip()

    m5424 = _RFC5424_RE.match(raw)
    if m5424:
        pri = int(m5424.group("pri"))
        facility, severity = pri // 8, pri % 8
        ts = None
        try:
            ts_str = m5424.group("ts")
            if ts_str != "-":
                ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            ts = None
        return ParsedSyslog(
            facility=facility,
            severity=SyslogSeverity(severity),
            hostname=m5424.group("host") if m5424.group("host") != "-" else None,
            tag=m5424.group("app") if m5424.group("app") != "-" else None,
            message=m5424.group("msg"),
            device_reported_at=ts,
        )

    m3164 = _RFC3164_RE.match(raw)
    if m3164:
        pri = int(m3164.group("pri"))
        facility, severity = pri // 8, pri % 8
        return ParsedSyslog(
            facility=facility,
            severity=SyslogSeverity(severity),
            hostname=m3164.group("host"),
            tag=m3164.group("tag").strip() or None,
            message=m3164.group("msg"),
            device_reported_at=_parse_rfc3164_timestamp(m3164.group("ts")),
        )

    # Fallback: pull PRI if present, treat the rest as one opaque message.
    pri_match = _PRI_RE.match(raw)
    if pri_match:
        pri = int(pri_match.group(1))
        facility, severity = pri // 8, pri % 8
        message = raw[pri_match.end():].strip()
    else:
        facility, severity, message = None, SyslogSeverity.INFORMATIONAL, raw

    return ParsedSyslog(
        facility=facility,
        severity=SyslogSeverity(severity) if isinstance(severity, int) else severity,
        hostname=None,
        tag=None,
        message=message,
        device_reported_at=None,
    )


# --- Correlation ---------------------------------------------------------

# (category, severity, regex-over-"TAG: message") -- matched in order,
# first match wins. This is deliberately a short, high-precision list of
# patterns that are worth an Alert row, not an attempt to categorize
# every syslog line (the overwhelming majority of syslog traffic is
# routine and shouldn't create alert noise). Patterns cover Cisco IOS/
# IOS-XE, Juniper Junos, Arista EOS, and generic Linux (sshd/PAM) message
# shapes, since those are exactly the vendor fleets this app manages
# (app.models.device.DeviceVendor).
CORRELATION_RULES: list[tuple[str, AlertSeverity, re.Pattern]] = [
    (
        "Auth Failure",
        AlertSeverity.WARNING,
        re.compile(
            r"authentication fail|login failed|failed password|invalid user|"
            r"%SEC_LOGIN-\d-FAILED|bad password attempt",
            re.IGNORECASE,
        ),
    ),
    (
        "ACL Deny",
        AlertSeverity.INFO,
        re.compile(r"%SEC-6-IPACCESSLOG|list \S+ denied|RT_FLOW_SESSION_DENY|packet-filter.*discard", re.IGNORECASE),
    ),
    (
        "Hardware Error",
        AlertSeverity.CRITICAL,
        re.compile(
            r"%PLATFORM.*FAULT|hardware error|power supply.*fail|fan.*fail|"
            r"CHASSISD_.*FRU_.*FAILED|temperature.*(critical|shutdown)|ECC error",
            re.IGNORECASE,
        ),
    ),
    (
        "Interface Down",
        AlertSeverity.WARNING,
        re.compile(r"%LINK-3-UPDOWN.*down|%LINEPROTO-5-UPDOWN.*down|SNMP_TRAP_LINK_DOWN|Ethernet.*down", re.IGNORECASE),
    ),
    (
        "Config Changed",
        AlertSeverity.INFO,
        re.compile(r"%SYS-5-CONFIG_I|configured from|UI_COMMIT|commit complete", re.IGNORECASE),
    ),
    (
        "Routing Adjacency Change",
        AlertSeverity.WARNING,
        re.compile(r"%OSPF-5-ADJCHG|%BGP-5-ADJCHANGE|bgp_\S*_down|OSPF.*neighbor.*down", re.IGNORECASE),
    ),
]

# Severity floor: even a matched pattern only raises an Alert if the
# syslog line's own severity is at least this urgent (numerically <=),
# so a device logging something at DEBUG/INFORMATIONAL that happens to
# contain matching text (e.g. a startup-config dump echoing old error
# text) doesn't manufacture an alert. WARNING (4) or worse.
_MIN_ALERT_SEVERITY = SyslogSeverity.WARNING


def _correlate(db: Session, msg: SyslogMessage) -> None:
    """Matches one just-persisted SyslogMessage against CORRELATION_RULES
    and, on a hit, raises/updates an Alert via the same dedup-aware path
    SNMP-poll-derived alerts use (alert_service.raise_alert) -- so a burst
    of repeated auth-failure lines from one device becomes a single
    escalating alert (occurrence_count climbing) instead of one alert row
    per syslog line.
    """
    haystack = f"{msg.tag or ''}: {msg.message}"
    for category, severity, pattern in CORRELATION_RULES:
        if not pattern.search(haystack):
            continue

        msg.correlated_category = category

        if msg.severity.value <= _MIN_ALERT_SEVERITY.value:
            alert, _is_new = alert_service.raise_alert(
                db,
                device_id=msg.device_id,
                severity=severity,
                source=AlertSource.SYSLOG,
                category=f"Syslog: {category}",
                message=f"{msg.reported_hostname or msg.source_ip}: {msg.message}",
            )
            msg.correlated_alert_id = alert.id
        return  # first match wins


def _resolve_device(db: Session, source_ip: str, reported_hostname: str | None) -> Device | None:
    """Matches an inbound syslog packet to a managed device: by
    management IP first (the reliable signal -- source_ip is whatever
    address the packet actually arrived from), falling back to a
    hostname match against the HOSTNAME field for senders relayed
    through something that rewrites the source IP (e.g. a syslog relay/
    aggregator), since the device's own reported hostname is still
    meaningful in that case even though the IP isn't.
    """
    device = db.query(Device).filter(Device.ip_address == source_ip).first()
    if device is not None:
        return device
    if reported_hostname:
        return db.query(Device).filter(Device.hostname == reported_hostname).first()
    return None


def ingest_message(db: Session, *, source_ip: str, raw: str) -> SyslogMessage:
    """Parses, persists, and correlates one syslog line. This is the
    single funnel both the UDP listener and POST /syslog/ingest call
    through, so parsing/correlation behavior never diverges between the
    two transports.
    """
    parsed = parse_syslog_line(raw)
    device = _resolve_device(db, source_ip, parsed.hostname)

    msg = SyslogMessage(
        device_id=device.id if device else None,
        source_ip=source_ip,
        facility=parsed.facility,
        severity=parsed.severity,
        reported_hostname=parsed.hostname,
        tag=parsed.tag,
        message=parsed.message,
        raw=raw,
        device_reported_at=parsed.device_reported_at,
    )
    db.add(msg)
    db.flush()  # need msg.id before correlation can attach correlated_alert_id

    _correlate(db, msg)

    db.commit()
    db.refresh(msg)

    event_bus.publish_event(
        "syslog_received",
        severity=msg.severity.name,
        category=msg.correlated_category,
        device_id=str(msg.device_id) if msg.device_id else None,
    )
    return msg


# --- UDP listener --------------------------------------------------------


class SyslogUDPProtocol(asyncio.DatagramProtocol):
    """Minimal asyncio UDP syslog receiver. Each datagram is one syslog
    message (per RFC 3164/5424 over UDP -- unlike TCP syslog there's no
    framing to worry about, the packet boundary *is* the message
    boundary). DB work is dispatched onto a worker thread so a slow
    ingest (correlation + alert dedup query) never blocks the event loop
    that's also receiving the next packet.
    """

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        source_ip = addr[0]
        try:
            raw = data.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - a malformed packet must never crash the listener
            return
        asyncio.create_task(self._handle(source_ip, raw))

    async def _handle(self, source_ip: str, raw: str) -> None:
        await asyncio.to_thread(_ingest_sync, source_ip, raw)


def _ingest_sync(source_ip: str, raw: str) -> None:
    db = SessionLocal()
    try:
        ingest_message(db, source_ip=source_ip, raw=raw)
    except Exception:  # noqa: BLE001 - one bad/malformed message must not take down the listener
        logger.exception("Failed to ingest syslog message from %s", source_ip)
    finally:
        db.close()


async def start_syslog_listener(host: str = "0.0.0.0", port: int | None = None) -> asyncio.DatagramTransport | None:
    """Starts the UDP syslog listener, returning its transport (so
    app.main can close it on shutdown) or None if binding failed --
    binding failure (e.g. port already in use, or <1024 without
    privileges) is logged, not raised, so a syslog port conflict never
    prevents the rest of the API from starting up.
    """
    port = port or settings.SYSLOG_UDP_PORT
    loop = asyncio.get_running_loop()
    try:
        transport, _protocol = await loop.create_datagram_endpoint(
            SyslogUDPProtocol, local_addr=(host, port)
        )
        logger.info("Syslog UDP listener started on %s:%s", host, port)
        return transport
    except OSError:
        logger.exception(
            "Could not bind syslog UDP listener on %s:%s -- syslog will still be "
            "reachable via POST /syslog/ingest, but the raw UDP port is unavailable.",
            host,
            port,
        )
        return None


# --- Query helpers (for the API layer) -----------------------------------


def fleet_syslog_summary(db: Session, *, since: datetime.datetime) -> dict:
    """Counts + hourly volume for the Syslog view's summary strip."""
    base = db.query(SyslogMessage).filter(SyslogMessage.received_at >= since)
    total = base.count()
    by_severity = {
        s.name.lower(): base.filter(SyslogMessage.severity == s).count() for s in SyslogSeverity
    }
    correlated = base.filter(SyslogMessage.correlated_category.isnot(None)).count()

    hourly_rows = (
        db.query(
            func.date_trunc("hour", SyslogMessage.received_at).label("hour"),
            func.count(SyslogMessage.id).label("count"),
        )
        .filter(SyslogMessage.received_at >= since)
        .group_by("hour")
        .order_by("hour")
        .all()
    )
    volume_by_hour = [{"hour": row.hour.isoformat(), "count": row.count} for row in hourly_rows]

    return {
        "total": total,
        "correlated": correlated,
        "by_severity": by_severity,
        "volume_by_hour": volume_by_hour,
    }
import enum
import uuid

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class SyslogSeverity(int, enum.Enum):
    """RFC 5424 / 3164 severity levels (0 = most severe). Kept as the
    numeric values themselves (not 0..7 renamed) since that's what's on
    the wire and what operators expect ("severity 3" == "error") --
    translating to a friendlier label is a display-layer concern (see
    SEVERITY_LABELS in the API schema), not a storage concern.
    """

    EMERGENCY = 0
    ALERT = 1
    CRITICAL = 2
    ERROR = 3
    WARNING = 4
    NOTICE = 5
    INFORMATIONAL = 6
    DEBUG = 7


class SyslogMessage(Base):
    """One received syslog message (RFC 3164 "BSD syslog" or RFC 5424).

    This is the data-completeness gap flagged against Auvik: SNMP polling
    and flow/interface counters only tell you a device's *numeric*
    state (CPU, errors, bandwidth) -- auth failures, hardware error
    events, ACL deny hits, and most vendor-specific fault conditions are
    only ever emitted as syslog, never exposed via a pollable OID. A
    device can look fully green on the Health Dashboard while its
    console is logging repeated auth failures or a failing PSU that has
    no MIB counter behind it at all.

    One row per received message (not deduplicated/aggregated at write
    time) so the raw feed and any future aggregation window are both
    derivable from the same table -- see syslog_service.ingest_message
    for the correlation step that also raises/updates an Alert row for
    messages matching a known-significant pattern, using the exact same
    dedup-by-category mechanism as SNMP-poll-derived alerts
    (app.services.alert_service.raise_alert), so a flapping "auth
    failure" flood shows up as one escalating alert, not a thousand.
    """

    __tablename__ = "syslog_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Best-effort match against known device inventory by the packet's
    # source IP (see syslog_service._resolve_device) -- nullable because a
    # syslog sender (e.g. a host/appliance not yet in inventory) can still
    # be worth capturing even before/without a matching Device row.
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True, index=True)
    source_ip = Column(String, nullable=False, index=True)

    # PRI-derived fields (RFC 3164 section 4.1.1 / RFC 5424 section 6.2.1):
    # PRI = facility * 8 + severity.
    facility = Column(Integer, nullable=True)  # 0-23, e.g. 4 = auth, 10 = authpriv, 16-23 = local0-7
    severity = Column(Enum(SyslogSeverity), nullable=False, default=SyslogSeverity.INFORMATIONAL, index=True)

    reported_hostname = Column(String, nullable=True)  # HOSTNAME field as the device itself reported it
    tag = Column(String, nullable=True)  # BSD TAG / RFC5424 APP-NAME, e.g. "sshd", "dhcpd", "%SEC-6-IPACCESSLOGP"
    message = Column(Text, nullable=False)  # parsed MSG content
    raw = Column(Text, nullable=False)  # untouched original line, kept for audit/troubleshooting

    device_reported_at = Column(DateTime(timezone=True), nullable=True)  # timestamp embedded in the message, if parsed
    received_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    # Correlation outcome (see syslog_service.CORRELATION_RULES): the
    # category name of whichever rule matched this message's tag/message
    # text, or NULL if nothing matched (the overwhelming majority of
    # routine/informational syslog traffic). Populated at ingest time so
    # the UI can filter "show me only the syslog lines that mattered"
    # without re-running regexes against the full text on every page load.
    correlated_category = Column(String, nullable=True, index=True)
    correlated_alert_id = Column(UUID(as_uuid=True), ForeignKey("alerts.id"), nullable=True)
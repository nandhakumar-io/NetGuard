"""RemoteSyslogDestination -- outbound syslog forwarding targets.

NetGuard already *receives* syslog from managed devices (see
app.services.syslog_service / app.models.syslog_message). This is the
other direction: NetGuard itself acting as a syslog *sender*, forwarding
its own alerts/events to an external log collector (Splunk, Graylog,
rsyslog, SolarWinds, etc.) over the wire in standard RFC 3164/5424
syslog format -- the same fan-out shape as WebhookEndpoint, just a UDP/
TCP syslog datagram instead of an HTTP POST. See
app.services.syslog_forward_service.
"""
import enum
import uuid

from sqlalchemy import Boolean, Column, DateTime, Enum, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class SyslogProtocol(str, enum.Enum):
    UDP = "udp"
    TCP = "tcp"


class SyslogDestination(Base):
    __tablename__ = "syslog_destinations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    host = Column(String, nullable=False)
    port = Column(Integer, nullable=False, default=514)
    protocol = Column(Enum(SyslogProtocol), nullable=False, default=SyslogProtocol.UDP)

    # Standard syslog facility number (0-23, default 16 = local0) and
    # minimum severity to forward ("info" | "warning" | "critical" --
    # same three-tier scale notification_service already uses
    # everywhere else, mapped to syslog severities in the service).
    facility = Column(Integer, nullable=False, default=16)
    min_severity = Column(String, nullable=False, default="info")

    # RFC 5424 vs legacy RFC 3164 framing. 5424 is preferred by most
    # modern collectors (Graylog, Splunk HEC-adjacent syslog inputs);
    # 3164 remains the lowest-common-denominator default for older
    # appliances (many SolarWinds/Kiwi Syslog setups still expect it).
    use_rfc5424 = Column(Boolean, nullable=False, default=False, server_default="false")

    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    last_sent_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    last_error_at = Column(DateTime(timezone=True), nullable=True)

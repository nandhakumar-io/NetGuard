import enum
import uuid

from sqlalchemy import Boolean, Column, DateTime, Enum, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class ProtocolName(str, enum.Enum):
    NETCONF = "netconf"
    RESTCONF = "restconf"
    SNMP = "snmp"


class ProtocolOperation(Base):
    """Detailed record of one protocol-level operation (NETCONF edit-config,
    RESTCONF PATCH, SNMP poll/trap, ...). Complements the coarser-grained
    AuditLog: this table keeps the full request/response payloads needed
    to debug a specific push, while AuditLog stays the lightweight
    who/what/when trail surfaced across every module.
    """

    __tablename__ = "protocol_operations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True, index=True)

    protocol = Column(Enum(ProtocolName), nullable=False)
    operation = Column(String, nullable=False)  # e.g. "edit-config", "PATCH", "health-poll"
    operator = Column(String, nullable=False, default="system")

    request_payload = Column(Text, nullable=True)  # request XML (NETCONF) or JSON body (RESTCONF)
    response_payload = Column(Text, nullable=True)  # response XML/JSON
    http_status = Column(Integer, nullable=True)  # RESTCONF only

    success = Column(Boolean, nullable=False, default=False)
    error_message = Column(Text, nullable=True)
    execution_time_ms = Column(Float, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
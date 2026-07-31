import enum
import uuid

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class AlertSeverity(str, enum.Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class AlertSource(str, enum.Enum):
    SNMP_TRAP = "snmp_trap"
    HEALTH_POLL = "health_poll"
    DRIFT = "drift"


class Alert(Base):
    """Alert Engine record. Generated either from an inbound SNMP trap
    (POST /snmp/traps) or from threshold breaches found during a routine
    SNMP health poll (app.services.snmp_service.evaluate_thresholds).
    """

    __tablename__ = "alerts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True, index=True)

    severity = Column(Enum(AlertSeverity), nullable=False, default=AlertSeverity.INFO)
    source = Column(Enum(AlertSource), nullable=False, default=AlertSource.HEALTH_POLL)
    category = Column(String, nullable=False)  # e.g. "Interface Down", "High CPU", "Temperature Critical"
    message = Column(Text, nullable=False)

    acknowledged = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
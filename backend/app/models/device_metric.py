import enum
import uuid

from sqlalchemy import Column, DateTime, Enum, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class HealthColor(str, enum.Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class DeviceMetric(Base):
    """One SNMP health-poll snapshot for a device (SNMP Health Dashboard).

    A new row is written on every poll (default every
    settings.SNMP_POLL_INTERVAL_SECONDS, see app.tasks.snmp_poll_task) so
    the dashboard can chart history, not just show the latest value.
    """

    __tablename__ = "device_metrics"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False, index=True)

    cpu_utilization_pct = Column(Float, nullable=True)
    memory_utilization_pct = Column(Float, nullable=True)
    interface_utilization_pct = Column(Float, nullable=True)
    interface_errors = Column(Integer, nullable=True)
    temperature_celsius = Column(Float, nullable=True)
    fan_status = Column(String, nullable=True)  # ok | failed | unknown
    power_supply_status = Column(String, nullable=True)  # ok | failed | unknown
    uptime_seconds = Column(Integer, nullable=True)

    # 0-100 composite score derived from the readings above (see
    # app.services.snmp_service.compute_health_score) plus the traffic-light
    # classification the SNMP Health Dashboard cards key off of.
    health_score = Column(Integer, nullable=True)
    health_color = Column(Enum(HealthColor), nullable=True)

    polled_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
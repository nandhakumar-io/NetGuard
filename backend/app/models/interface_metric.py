import uuid

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class InterfaceMetric(Base):
    """
    Latest per-interface bandwidth/error reading -- the per-link
    counterpart to DeviceMetric's whole-device figure.
    """

    __tablename__ = "interface_metrics"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False, index=True)

    if_index = Column(String, nullable=False, index=True)
    if_descr = Column(String, nullable=True)

    octets_total = Column(BigInteger, nullable=True)
    speed_bps = Column(BigInteger, nullable=True)
    errors = Column(Integer, nullable=True)

    utilization_pct = Column(Float, nullable=True)
    error_delta = Column(Integer, nullable=True)

    polled_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

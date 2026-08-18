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

    # Which collection path produced this row: "snmp" (default, polled on
    # SNMP_POLL_INTERVAL_SECONDS -- unchanged historical behavior) or
    # "gnmi" (pushed by app.services.gnmi_service off a device's own
    # SUBSCRIBE sample interval, typically sub-second to a few seconds).
    # Lets the Health/Interface UI and any query distinguish "this
    # reading is fresh because a poll happened to land 3s ago" from
    # "this reading is fresh because the device is actively streaming" --
    # and lets a device with both SNMP and gNMI enabled keep both series
    # instead of one silently overwriting the other.
    source = Column(String, nullable=False, default="snmp", server_default="snmp")

    polled_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

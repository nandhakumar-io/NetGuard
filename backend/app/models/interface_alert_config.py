
from sqlalchemy import Boolean, Column, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class InterfaceAlertConfig(Base):
    """Device interface alert configuration (opt-out list).
    By default, SNMP linkUpDown and health-poll interface checks generate
    alerts. If `enabled` is False for a specific device + if_descr, those
    alerts are suppressed at generation time.
    """

    __tablename__ = "interface_alert_configs"

    device_id = Column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    if_descr = Column(String, primary_key=True)
    enabled = Column(Boolean, nullable=False, default=True)

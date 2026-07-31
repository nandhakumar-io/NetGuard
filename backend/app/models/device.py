import enum
import uuid

from sqlalchemy import Column, String, Enum, DateTime, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class DeviceVendor(str, enum.Enum):
    CISCO = "cisco"
    JUNIPER = "juniper"
    ARISTA = "arista"
    LINUX = "linux"


class DeviceStatus(str, enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class Device(Base):
    __tablename__ = "devices"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hostname = Column(String, unique=True, index=True, nullable=False)
    ip_address = Column(String, nullable=False)
    vendor = Column(Enum(DeviceVendor), nullable=False, default=DeviceVendor.CISCO)
    site = Column(String, nullable=True)
    device_type = Column(String, nullable=True)  # e.g. router, switch, firewall
    status = Column(Enum(DeviceStatus), nullable=False, default=DeviceStatus.UNKNOWN)
    ssh_username = Column(String, nullable=True)
    ssh_credential_ref = Column(String, nullable=True)  # pointer to secret store, not raw secret
    created_at = Column(DateTime(timezone=True), server_default=func.now())

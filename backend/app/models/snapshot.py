import uuid

from sqlalchemy import Column, String, DateTime, Text, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class ConfigSnapshot(Base):
    """Immutable, encrypted backup of a device configuration taken before deployment."""

    __tablename__ = "config_snapshots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False)
    change_request_id = Column(UUID(as_uuid=True), ForeignKey("change_requests.id"), nullable=True)

    running_config_encrypted = Column(Text, nullable=False)
    startup_config_encrypted = Column(Text, nullable=True)
    checksum = Column(String, nullable=False)
    version = Column(String, nullable=False)  # e.g. incrementing version or git-style hash

    created_at = Column(DateTime(timezone=True), server_default=func.now())

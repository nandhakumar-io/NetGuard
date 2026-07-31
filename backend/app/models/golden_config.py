import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class GoldenConfig(Base):
    """The authoritative, approved-baseline configuration for a device.

    One row per device (device_id is unique). Used as the comparison
    target for Configuration Management (GET/PUT .../golden-config,
    POST .../compare) and for Drift Detection when
    ConfigDrift.baseline == DriftBaseline.GOLDEN_CONFIG.

    Encrypted at rest the same way as ConfigSnapshot (see
    app.services.snapshot_service, reused rather than duplicated here).
    """

    __tablename__ = "golden_configs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False, unique=True, index=True)

    config_encrypted = Column(Text, nullable=False)
    checksum = Column(String, nullable=False)
    set_by = Column(String, nullable=False, default="system")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
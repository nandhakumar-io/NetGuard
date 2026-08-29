import uuid

from sqlalchemy import Column, DateTime, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class PhysicalLocation(Base):
    """An empty block/data-center/rack placeholder -- gives a physical
    tier a row independent of Device.block/data_center/rack membership,
    so it can be created, renamed, or deleted before (or after) any
    device is tagged into it. See alembic/versions/0106_physical_locations.py
    and app.api.devices.get_device_groups, which merges these rows into
    the same tree it builds from live devices.

    A row with data_center/rack both NULL represents a bare block. A row
    with data_center set and rack NULL represents a data center within
    that block. A row with all three set represents a rack. This mirrors
    how a device implicitly "creates" each tier of its own block/
    data_center/rack -- there's no separate row per already-populated
    tier, only for the otherwise-empty ones this table exists to give a
    row.
    """

    __tablename__ = "physical_locations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    block = Column(String, nullable=False, index=True)
    data_center = Column(String, nullable=True, index=True)
    rack = Column(String, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("block", "data_center", "rack", name="uq_physical_location_tier"),
    )

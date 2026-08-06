import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class DeviceGroup(Base):
    """A named, user-managed device group -- distinct from the free-text
    data_center/rack fields on Device (see app.services.topology_service
    and the Groups page's "Data Center / Rack" view). Those two fields are
    physical-location grouping; DeviceGroup is logical grouping an admin
    defines explicitly ("Edge Firewalls", "Branch Routers - East Region",
    "Q3 Migration Batch"), with optional nesting via parent_group_id so a
    broad group can contain narrower sub-groups (e.g. "All Switches" ->
    "Core Switches" / "Access Switches").

    group_type is free-text like Device.device_type/device_role -- kept
    open-ended (e.g. "static", "site", "role", "project") rather than an
    enum since orgs organize fleets differently and this app doesn't
    enforce a taxonomy on any of its other grouping fields either.
    """

    __tablename__ = "device_groups"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False, index=True)
    description = Column(String, nullable=True)
    group_type = Column(String, nullable=False, default="static", server_default="static")
    parent_group_id = Column(UUID(as_uuid=True), ForeignKey("device_groups.id"), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
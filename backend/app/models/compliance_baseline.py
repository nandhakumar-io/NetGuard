import uuid

from sqlalchemy import Column, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class ComplianceBaseline(Base):
    """An approved-baseline configuration *template* shared by every device
    of a given role (e.g. "core", "access", "edge-firewall") -- the
    role-based counterpart to GoldenConfig, which is one-per-device.

    Existed as a gap before this: a fleet's core switches and access
    switches were forced to either share the exact same GoldenConfig (a
    core switch's BGP/OSPF/uplink config flagged as "drift" on every
    access switch that doesn't have it, and vice versa) or go without any
    baseline at all and rely purely on PREVIOUS_BACKUP comparisons, which
    only catch *changes*, not "config non-compliant with what this role is
    supposed to look like". One row per role (role is unique) -- like
    GoldenConfig, this is a single current approved state per role, not a
    history; see app.services.drift_service._resolve_baseline_config for
    how DriftBaseline.ROLE_BASELINE resolves a device to its role's row via
    Device.device_role.

    Encrypted at rest the same way as GoldenConfig/ConfigSnapshot (see
    app.services.snapshot_service, reused rather than duplicated here).
    """

    __tablename__ = "compliance_baselines"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Free-text role key, matching Device.device_role (e.g. "core",
    # "distribution", "access", "edge-firewall", "wan-edge") -- not an
    # enum, since operators name roles however their org does and the set
    # of roles varies fleet to fleet.
    device_role = Column(String, nullable=False, unique=True, index=True)

    config_encrypted = Column(Text, nullable=False)
    checksum = Column(String, nullable=False)
    set_by = Column(String, nullable=False, default="system")
    description = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

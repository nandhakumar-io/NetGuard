import enum
import uuid

from sqlalchemy import Column, String, Enum, DateTime, Text, Integer, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class DriftSeverity(str, enum.Enum):
    NONE = "none"          # compared, no differences found
    LOW = "low"            # a handful of lines changed (e.g. clock, banner)
    MEDIUM = "medium"      # a meaningful block of config changed
    HIGH = "high"          # large-scale or security-relevant change


class ConfigDrift(Base):
    """A single drift check result: the live running-config on a device,
    compared against the most recent ConfigSnapshot NetGuard has on file
    for it. A non-trivial diff means someone/something changed the device
    outside of NetGuard's approved change-request workflow.
    """

    __tablename__ = "config_drifts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False)
    baseline_snapshot_id = Column(UUID(as_uuid=True), ForeignKey("config_snapshots.id"), nullable=True)

    drifted = Column(String, nullable=False, default="false")  # "true" / "false" (matches HealthCheckResult convention)
    severity = Column(Enum(DriftSeverity), nullable=False, default=DriftSeverity.NONE)
    lines_changed = Column(Integer, nullable=False, default=0)
    diff = Column(Text, nullable=True)  # unified diff, baseline -> live (empty when not drifted)
    detail = Column(Text, nullable=True)  # human-readable summary, or error detail on check failure

    triggered_by = Column(String, nullable=False, default="scheduled")  # "scheduled" | "pre_deployment" | "manual"

    resolved = Column(String, nullable=False, default="false")
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolved_by = Column(String, nullable=True)

    checked_at = Column(DateTime(timezone=True), server_default=func.now())

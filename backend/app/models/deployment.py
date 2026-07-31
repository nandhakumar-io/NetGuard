import enum
import uuid

from sqlalchemy import Column, String, Enum, DateTime, Text, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class DeploymentStatus(str, enum.Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class Deployment(Base):
    __tablename__ = "deployments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    change_request_id = Column(UUID(as_uuid=True), ForeignKey("change_requests.id"), nullable=False)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False)
    snapshot_id = Column(UUID(as_uuid=True), ForeignKey("config_snapshots.id"), nullable=True)

    protocol = Column(String, nullable=False, default="ssh")  # ssh | netconf | restconf | napalm
    status = Column(Enum(DeploymentStatus), nullable=False, default=DeploymentStatus.QUEUED)
    error_message = Column(Text, nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class HealthCheckResult(Base):
    __tablename__ = "health_check_results"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deployment_id = Column(UUID(as_uuid=True), ForeignKey("deployments.id"), nullable=False)

    category = Column(String, nullable=False)  # infrastructure | routing | services | traffic
    check_name = Column(String, nullable=False)  # e.g. "ping", "bgp_neighbor", "dns"
    passed = Column(String, nullable=False)  # "true" / "false" (kept simple for SQLite/Postgres portability)
    detail = Column(Text, nullable=True)

    checked_at = Column(DateTime(timezone=True), server_default=func.now())

import enum
import uuid

from sqlalchemy import Column, String, Enum, DateTime, Text, Integer, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class ChangeStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    VALIDATING = "validating"
    DEPLOYING = "deploying"
    MONITORING = "monitoring"
    SUCCESS = "success"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class ChangePriority(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EMERGENCY = "emergency"


class ChangeRequest(Base):
    __tablename__ = "change_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False)
    # JSON-encoded list of extra device UUIDs (SRS 6.6 multi-device deployment).
    # Stored as text rather than a join table to keep the prototype schema
    # simple; see pipeline_service.target_device_ids() for the reader side.
    additional_device_ids = Column(Text, nullable=True)
    submitted_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    priority = Column(Enum(ChangePriority), nullable=False, default=ChangePriority.MEDIUM)
    description = Column(Text, nullable=False)
    business_justification = Column(Text, nullable=True)
    maintenance_window_start = Column(DateTime(timezone=True), nullable=True)
    maintenance_window_end = Column(DateTime(timezone=True), nullable=True)

    current_config = Column(Text, nullable=True)
    proposed_config = Column(Text, nullable=False)
    config_diff = Column(Text, nullable=True)

    risk_score = Column(Integer, nullable=True)  # 0-100, set by AI Configuration Analyzer
    risk_findings = Column(Text, nullable=True)  # JSON-encoded list of detected risks

    status = Column(Enum(ChangeStatus), nullable=False, default=ChangeStatus.DRAFT)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
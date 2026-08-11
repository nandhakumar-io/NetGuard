import enum
import uuid

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class ApprovalStageType(enum.Enum):
    PEER_REVIEW = "peer_review"
    MANAGER_SIGNOFF = "manager_signoff"
    ADMIN_APPROVAL = "admin_approval"


class ApprovalStageStatus(enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# Typical defaults for NetGuard roles. approval_chain_service operates on
# these strings regardless of what UserRole enum actually defines.
STAGE_ELIGIBLE_ROLES = {
    ApprovalStageType.PEER_REVIEW: ["network_engineer", "network_admin"],
    ApprovalStageType.MANAGER_SIGNOFF: ["manager", "network_admin"],
    ApprovalStageType.ADMIN_APPROVAL: ["network_admin"],
}


class ChangeRequestApprovalStage(Base):
    __tablename__ = "change_request_approval_stages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    change_request_id = Column(UUID(as_uuid=True), ForeignKey("change_requests.id", ondelete="CASCADE"), nullable=False)
    sequence = Column(Integer, nullable=False)
    stage_type = Column(Enum(ApprovalStageType), nullable=False)
    required_role = Column(String, nullable=False)
    status = Column(Enum(ApprovalStageStatus), nullable=False, default=ApprovalStageStatus.PENDING)

    acted_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    acted_at = Column(DateTime(timezone=True), nullable=True)
    notes = Column(String, nullable=True)

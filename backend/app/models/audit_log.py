import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class AuditLog(Base):
    """Append-only audit trail. Rows are never updated or deleted at the application layer."""

    __tablename__ = "audit_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor = Column(String, nullable=False)  # user email or "system"
    action = Column(String, nullable=False)  # e.g. "Submitted CR", "Approved", "Deployment"
    device_hostname = Column(String, nullable=True)
    result = Column(String, nullable=False)  # e.g. "Success", "Failed", "Approved"
    change_request_id = Column(UUID(as_uuid=True), ForeignKey("change_requests.id"), nullable=True)
    detail = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

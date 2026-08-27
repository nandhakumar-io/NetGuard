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

    # Tenant scoping (migration 0097_audit_log_tenant_and_rule_inheritance).
    # NULL = global/system event (login, MSP-staff action, or a row whose
    # originating device/tenant couldn't be determined at write time) --
    # same "NULL = global" convention as AlertRule.tenant_id /
    # WebhookEndpoint.tenant_id. Every write site should pass this
    # explicitly via app.services.audit_service.record_event rather than
    # relying on backfill; see app.core.deps.get_tenant_scope for how
    # reads are filtered.
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

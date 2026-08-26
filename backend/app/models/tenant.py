import uuid

from sqlalchemy import Boolean, Column, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class Tenant(Base):
    """A managed customer/organization whose devices, alerts, and users are
    scoped to it.

    Added retroactively (see migration 0092_tenants) on top of what had
    been a genuinely single-tenant app -- every pre-existing Device/User
    row is backfilled onto one "Default" Tenant so nothing breaks, and
    normal staff continue to only ever see their own tenant's data via
    app.core.deps.get_current_tenant_id.

    MSP staff (User.is_msp_staff) are the one exception: they aren't
    scoped to any single tenant and are the intended audience for the
    cross-tenant NOC board (app.api.tenant_board), which is the whole
    reason this model exists.
    """

    __tablename__ = "tenants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    # Short URL/API-friendly identifier, e.g. "acme-corp" -- unique so it
    # can double as a stable external reference (webhooks, ChatOps links)
    # independent of the display name, which admins can rename freely.
    slug = Column(String, unique=True, index=True, nullable=False)

    is_active = Column(Boolean, nullable=False, default=True, server_default="true")

    created_at = Column(DateTime(timezone=True), server_default=func.now())

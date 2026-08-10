import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class DashboardPreference(Base):
    """One row per user: their saved dashboard widget layout (see
    app.services.dashboard_widgets.merge_layout for how this JSON blob is
    reconciled against the registry). `layout` is a JSON-encoded
    list[{"id": str, "visible": bool}], stored as text rather than a
    native JSON column to keep this portable across the Postgres/SQLite
    split already used elsewhere in the model layer (tests run on SQLite).
    """

    __tablename__ = "dashboard_preferences"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, unique=True)
    layout = Column(Text, nullable=False, default="[]", server_default="[]")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

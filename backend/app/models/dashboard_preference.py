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
    # JSON-encoded dict of per-metric warn/critical bands, e.g.
    # {"cpu": {"warn": 70, "critical": 90}, "memory": {...}, "bandwidth": {...}}.
    # Drives the color bands on the CPU/RAM/HEALTH gauges and the Top
    # CPU/Memory/Bandwidth widgets (see app.services.dashboard_widgets.
    # merge_thresholds) instead of the hardcoded bands every admin
    # previously got regardless of what "high" means for their fleet.
    # Text/JSON-encoded for the same Postgres/SQLite portability reason as
    # `layout` above. Missing/unset metrics fall back to the built-in
    # defaults in dashboard_widgets.DEFAULT_THRESHOLDS.
    thresholds = Column(Text, nullable=False, default="{}", server_default="{}")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

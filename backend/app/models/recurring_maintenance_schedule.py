"""Recurring/scheduled maintenance windows -- "patch Tuesdays", monthly
firmware windows, etc. -- so an operator defines the recurrence once
instead of creating a one-off MaintenanceWindow every time.

A schedule doesn't suppress alerts by itself: app.services.
recurring_window_service materializes it into concrete MaintenanceWindow
rows (same table/model the rest of the app already reads, including
alert_service's suppression check), tagged via MaintenanceWindow.
recurrence_id so they're recognizable as generated vs. one-off and can be
regenerated/cleaned up safely.
"""
import enum
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.database import Base


class RecurrenceFrequency(str, enum.Enum):
    WEEKLY = "weekly"
    # "Nth weekday of the month" -- e.g. day_of_week=Tuesday,
    # week_of_month=2 encodes the classic "second Tuesday" patch Tuesday
    # pattern. week_of_month=-1 means "last <weekday> of the month".
    MONTHLY = "monthly"


class RecurringMaintenanceSchedule(Base):
    __tablename__ = "recurring_maintenance_schedules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    name = Column(String, nullable=False)
    reason = Column(Text, nullable=True)

    # Same scope semantics as MaintenanceWindow.
    scope = Column(String, nullable=False, default="device")  # device | site | fleet
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True, index=True)
    site = Column(String, nullable=True, index=True)

    frequency = Column(Enum(RecurrenceFrequency), nullable=False)
    interval = Column(Integer, nullable=False, default=1)  # every N weeks/months
    day_of_week = Column(Integer, nullable=False)  # 0=Monday .. 6=Sunday
    # Only used when frequency=MONTHLY: which occurrence of day_of_week
    # in the month (1=first, 2=second, ... -1=last). NULL for WEEKLY.
    week_of_month = Column(Integer, nullable=True)

    start_time = Column(Time, nullable=False)  # local window start, UTC
    duration_minutes = Column(Integer, nullable=False, default=120)

    # Recurrence bounds -- generation never materializes a window outside
    # [active_from, active_until]. active_until=NULL means "indefinitely".
    active_from = Column(DateTime(timezone=True), nullable=False)
    active_until = Column(DateTime(timezone=True), nullable=True)

    enabled = Column(Boolean, nullable=False, default=True, server_default="true")

    created_by = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    device = relationship("Device")
    generated_windows = relationship("MaintenanceWindow", backref="recurrence", viewonly=True)

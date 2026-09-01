import enum
import uuid

from sqlalchemy import Boolean, Column, DateTime, Enum, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class OnCallRotationType(str, enum.Enum):
    """How primary/secondary alternate over time. NONE means
    primary_user_email is always the current contact (no rotation --
    e.g. a single dedicated on-call phone/pager alias)."""

    NONE = "none"
    DAILY = "daily"
    WEEKLY = "weekly"


class OnCallSchedule(Base):
    """A named on-call rotation an EscalationPolicy can point at instead
    of (or in addition to) a static secondary_contacts list -- see
    app.models.escalation_policy.EscalationPolicy.on_call_schedule_id
    and app.services.on_call_service.current_contact.

    Deliberately minimal (two named people, not an arbitrary roster):
    NetGuard is operated by small teams, so a primary/secondary pair
    with a rotation cadence covers the common case ("Alex this week,
    Priya next week, whoever isn't on call is the fallback") without
    the complexity of a full shift-scheduling system.
    """

    __tablename__ = "on_call_schedules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)

    primary_user_email = Column(String, nullable=False)
    secondary_user_email = Column(String, nullable=True)

    rotation_type = Column(
        Enum(OnCallRotationType, name="oncallrotationtype", values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        default=OnCallRotationType.NONE,
        server_default=OnCallRotationType.NONE.value,
    )
    # HH:MM (24h, in `timezone` below) that a DAILY/WEEKLY rotation flips
    # from whoever's currently on to the other person -- e.g. "09:00" so
    # handover happens at the start of a workday rather than at midnight
    # local time, which would flip mid-sleep for whoever's about to come
    # on call. NULL = flip at 00:00.
    shift_handover_time = Column(String, nullable=True)
    # IANA zone name (e.g. "America/New_York"). NULL = UTC.
    timezone = Column(String, nullable=True)

    enabled = Column(Boolean, nullable=False, default=True, server_default="true")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

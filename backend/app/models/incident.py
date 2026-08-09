"""Incident / postmortem tracking.

Alert Correlation (app.services.alert_correlation_service) already groups
a storm of alerts under one `root_cause_alert_id`, and Escalation
Policies already page someone when a critical alert sits unacknowledged.
Neither of those produces a durable, closed record of "what happened,
when, who worked it, and what we're changing so it doesn't happen
again" -- that's what Incident is for.

An Incident is opened (usually once the underlying alert group has been
resolved, i.e. after the fact, for the retro -- but nothing stops opening
one while still live) from a *correlated alert group*: the root-cause
Alert plus every Alert that has `root_cause_alert_id` pointing at it.
IncidentTimelineEvent gives that incident its own independent timeline
(detection, mitigation steps, resolution, follow-ups) distinct from the
raw alert/audit-log firehose, so a postmortem reads as a narrative
instead of a log dump.
"""
import enum
import uuid

from sqlalchemy import Column, DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.database import Base


class IncidentSeverity(str, enum.Enum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"


class IncidentStatus(str, enum.Enum):
    OPEN = "open"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"
    POSTMORTEM_DUE = "postmortem_due"
    CLOSED = "closed"


class Incident(Base):
    """A formal record built from a correlated alert group, for retros.

    `root_cause_alert_id` is the Alert that anchored the correlated group
    at creation time (see alert_correlation_service.ROOT_CAUSE_CATEGORIES)
    -- kept even after the alert itself resolves, since the incident
    record needs to outlive the alert it was built from. `alert_ids` is a
    JSON-encoded list of every Alert.id folded into this incident (the
    root cause plus everything it suppressed), captured at creation time
    rather than re-derived live, since the topology/suppression links on
    Alert can change shape (e.g. alerts get individually resolved) after
    the incident is opened -- the incident's scope shouldn't silently
    drift with them.
    """

    __tablename__ = "incidents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String, nullable=False)
    summary = Column(Text, nullable=True)

    severity = Column(Enum(IncidentSeverity), nullable=False, default=IncidentSeverity.MAJOR)
    status = Column(Enum(IncidentStatus), nullable=False, default=IncidentStatus.OPEN)

    root_cause_alert_id = Column(UUID(as_uuid=True), ForeignKey("alerts.id"), nullable=True, index=True)
    # JSON list of alert UUID strings (root cause + everything it suppressed).
    alert_ids = Column(Text, nullable=False, default="[]", server_default="[]")

    detected_at = Column(DateTime(timezone=True), nullable=True)
    mitigated_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)

    # Postmortem fields -- filled in once the incident is being written up.
    root_cause_summary = Column(Text, nullable=True)
    impact_summary = Column(Text, nullable=True)
    action_items = Column(Text, nullable=True)  # freeform; one per line in the UI

    created_by = Column(String, nullable=True)  # user email or "system:correlation"
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    timeline_events = relationship(
        "IncidentTimelineEvent", back_populates="incident",
        order_by="IncidentTimelineEvent.occurred_at", cascade="all, delete-orphan",
    )


class IncidentTimelineEvent(Base):
    """One narrative entry on an incident's timeline -- detection,
    mitigation steps, comms sent, resolution, follow-up -- independent of
    the raw Alert/AuditLog rows so a postmortem reads as a story rather
    than a re-export of the alert feed.
    """

    __tablename__ = "incident_timeline_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id = Column(UUID(as_uuid=True), ForeignKey("incidents.id"), nullable=False, index=True)

    event_type = Column(String, nullable=False, default="note")  # detection | mitigation | resolution | note | status_change
    description = Column(Text, nullable=False)
    actor = Column(String, nullable=True)  # user email or "system"

    occurred_at = Column(DateTime(timezone=True), server_default=func.now())

    incident = relationship("Incident", back_populates="timeline_events")

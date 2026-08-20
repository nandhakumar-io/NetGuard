import enum
import uuid

from sqlalchemy import Boolean, Column, DateTime, Enum, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class EscalationSeverityScope(str, enum.Enum):
    """Which alert severities this policy watches. Mirrors AlertSeverity
    plus an ALL catch-all so one policy can cover every severity without
    the operator having to create three near-identical policies."""

    CRITICAL = "critical"
    WARNING = "warning"
    ALL = "all"


class EscalationChannel(str, enum.Enum):
    """Where the escalation notice goes, on top of the normal in-app
    Notification Center row that notification_service.notify() always
    writes. EMAIL/WEBHOOK reuse notification_service's existing SMTP and
    generic-webhook plumbing; SLACK/TEAMS post straight to the policy's
    own webhook URL instead of the fleet-wide default ones, so a
    secondary on-call channel can differ from the primary noise channel.
    """

    EMAIL = "email"
    WEBHOOK = "webhook"
    SLACK = "slack"
    TEAMS = "teams"
    PUSH = "push"


class EscalationPolicy(Base):
    """Unacknowledged-alert escalation rule (NOC "if nobody acks this in
    N minutes, page someone else" pattern).

    Evaluated every ESCALATION_SWEEP_INTERVAL_SECONDS by
    app.tasks.run_escalation_sweep_task -> app.services.escalation_service
    against every currently active (unresolved, unacknowledged,
    non-suppressed) Alert: any alert matching this policy's severity scope
    that has been open longer than `unack_minutes` gets escalated once
    (Alert.escalated flips to True) and, if `repeat_minutes` is set,
    re-escalated on that cadence for as long as it stays unacknowledged.

    Deliberately alert-level, not device/category-scoped like AlertRule --
    escalation is about response time (how long has this sat unhandled),
    not about detection conditions, so it composes with every alert
    source (SNMP, syslog, drift, protocol failures, ...) without
    duplicating each one's own trigger logic.
    """

    __tablename__ = "escalation_policies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)

    severity_scope = Column(Enum(EscalationSeverityScope), nullable=False, default=EscalationSeverityScope.CRITICAL)

    # How long an alert can sit unacknowledged before this policy fires.
    unack_minutes = Column(Integer, nullable=False, default=15)

    # If set, an already-escalated-but-still-unacknowledged alert fires
    # this policy again every `repeat_minutes` (keeps paging until
    # someone acks) instead of escalating exactly once. NULL = one-shot.
    repeat_minutes = Column(Integer, nullable=True)

    # Secondary/on-call contact(s) to notify -- comma-separated email
    # addresses (EMAIL channel) and/or a single webhook URL
    # (WEBHOOK/SLACK/TEAMS channel; whichever the channel needs).
    secondary_contacts = Column(Text, nullable=True)  # comma-separated emails
    channel = Column(Enum(EscalationChannel), nullable=False, default=EscalationChannel.EMAIL)
    webhook_url = Column(String, nullable=True)

    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    created_by = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

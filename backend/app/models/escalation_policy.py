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
    func,
)
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
    # Ignored for EMAIL/PUSH once on_call_schedule_id is set -- see
    # app.services.on_call_service.current_contact -- but left in place
    # as the fallback contact if the schedule is later cleared, and it's
    # still what WEBHOOK/SLACK/TEAMS policies use to name a human in the
    # message body since those channels have no per-person routing.
    secondary_contacts = Column(Text, nullable=True)  # comma-separated emails
    # An On-Call Schedule (rotation) to resolve the current contact from
    # instead of a static secondary_contacts string -- e.g. "whoever's on
    # call this week" rather than always the same person. Takes priority
    # over secondary_contacts for EMAIL/PUSH channels when set; NULL means
    # "use secondary_contacts as a fixed list" (the original behavior).
    on_call_schedule_id = Column(
        UUID(as_uuid=True), ForeignKey("on_call_schedules.id", ondelete="SET NULL"), nullable=True
    )
    channel = Column(Enum(EscalationChannel), nullable=False, default=EscalationChannel.EMAIL)
    webhook_url = Column(String, nullable=True)

    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    created_by = Column(String, nullable=True)

    # Tenant scoping (migration 0098_escalation_policy_tenant_scoping).
    # NULL = global/MSP-authored policy, evaluated against every tenant's
    # alerts -- same "global unless scoped" convention as
    # AlertRule.tenant_id / WebhookEndpoint.tenant_id. See
    # app.core.deps.get_tenant_scope and app.services.escalation_service.
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True)

    # Inheritance: when set, this is a tenant-authored policy that
    # explicitly overrides the referenced global (tenant_id IS NULL)
    # policy -- either re-tuning it (different unack_minutes/contacts/
    # channel) or suppressing it for this tenant (enabled=False +
    # parent_policy_id set). Mirrors AlertRule.parent_rule_id; a tenant
    # policy with no parent_policy_id is additive alongside whatever
    # global policies apply, same as before this column existed.
    parent_policy_id = Column(
        UUID(as_uuid=True), ForeignKey("escalation_policies.id"), nullable=True, index=True
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

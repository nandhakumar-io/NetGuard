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
from sqlalchemy.orm import relationship

from app.core.database import Base


class AlertSeverity(str, enum.Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class AlertSource(str, enum.Enum):
    SNMP_TRAP = "snmp_trap"
    HEALTH_POLL = "health_poll"
    DRIFT = "drift"
    # Added for ProtocolManager integration (app.services.protocol_manager):
    # any failed NETCONF/RESTCONF/SSH operation raises one of these instead
    # of silently only recording a ProtocolOperation row.
    PROTOCOL_FAILURE = "protocol_failure"
    # Added for syslog collection/correlation (app.services.syslog_service):
    # an inbound syslog line matching a known-significant pattern (auth
    # failure, hardware error, ACL deny, ...) raises one of these -- the
    # one alert source that isn't SNMP-derived at all.
    SYSLOG = "syslog"


class Alert(Base):
    """Alert Engine record. Generated either from an inbound SNMP trap
    (POST /snmp/traps) or from threshold breaches found during a routine
    SNMP health poll (app.services.snmp_service.evaluate_thresholds).
    """

    __tablename__ = "alerts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True, index=True)

    severity = Column(Enum(AlertSeverity), nullable=False, default=AlertSeverity.INFO)
    source = Column(Enum(AlertSource), nullable=False, default=AlertSource.HEALTH_POLL)
    category = Column(String, nullable=False)  # e.g. "Interface Down", "High CPU", "Temperature Critical"
    message = Column(Text, nullable=False)

    acknowledged = Column(Boolean, nullable=False, default=False, server_default="false")
    acknowledged_by = Column(String, nullable=True)

    resolved = Column(Boolean, nullable=False, default=False, server_default="false")
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolved_by = Column(String, nullable=True)

    # Dedup support (see app.services.alert_service.raise_alert): rather
    # than inserting a brand-new row every time a poll/check finds the
    # same still-active condition, an existing unresolved alert for the
    # same device_id+category is updated in place. Without this, clearing
    # alerts looked broken -- the next poll cycle would immediately
    # re-create duplicates for any condition that hadn't actually gone
    # away yet.
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    occurrence_count = Column(Integer, nullable=False, default=1, server_default="1")

    # Topology-aware correlation (app.services.alert_correlation_service):
    # when a device drops off the network entirely (Device Unreachable,
    # critical), every device that's only reachable *through* it also
    # starts firing its own alerts a poll cycle or two later -- without
    # this, a single core-switch failure looks like a storm of unrelated
    # alerts instead of one root cause. `suppressed` alerts are still
    # real, still stored, and still resolvable individually; they're just
    # flagged as a likely *consequence* of `root_cause_alert_id` so the
    # UI can collapse them under it instead of listing them as equally
    # urgent, independent problems.
    root_cause_alert_id = Column(UUID(as_uuid=True), ForeignKey("alerts.id"), nullable=True, index=True)
    suppressed = Column(Boolean, nullable=False, default=False, server_default="false")

    # Maintenance-window suppression (app.services.alert_service +
    # app.models.maintenance_window): set when this alert was raised/
    # updated while an active maintenance window covered its device.
    # Distinct from `suppressed` above (that's topology-correlation
    # "consequence of another alert"); this one is "expected noise during
    # planned work". Still stored and independently resolvable -- just
    # excluded from the default Active Alerts view and from notification
    # fan-out.
    suppressed_by_window_id = Column(UUID(as_uuid=True), ForeignKey("maintenance_windows.id"), nullable=True, index=True)

    # Per-alert snooze/mute (app.models.alert_snooze.AlertSnooze): set when
    # a matching active snooze covers this alert's device_id and/or category.
    # NULL = not muted; non-NULL = muted by that snooze (checked against
    # AlertSnooze.expires_at to determine if it's still active). Added by
    # migration 0034.
    muted_by_snooze_id = Column(UUID(as_uuid=True), ForeignKey("alert_snoozes.id"), nullable=True, index=True)

    # Escalation Policies (app.models.escalation_policy +
    # app.services.escalation_service): set the first time this alert
    # breaches an enabled policy's unack_minutes threshold while still
    # unacknowledged/unresolved. `last_escalated_at` is the same as
    # `escalated_at` for a one-shot policy, but keeps advancing on every
    # repeat firing for a policy with `repeat_minutes` set, so the sweep
    # can tell "already escalated once, no repeat due yet" apart from
    # "repeat window elapsed, fire again" without a second table.
    escalated = Column(Boolean, nullable=False, default=False, server_default="false")
    escalated_at = Column(DateTime(timezone=True), nullable=True)
    last_escalated_at = Column(DateTime(timezone=True), nullable=True)
    escalation_policy_id = Column(UUID(as_uuid=True), ForeignKey("escalation_policies.id"), nullable=True, index=True)
    escalation_count = Column(Integer, nullable=False, default=0, server_default="0")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    root_cause = relationship("Alert", remote_side=[id], foreign_keys=[root_cause_alert_id])

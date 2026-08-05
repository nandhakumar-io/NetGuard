import enum
import uuid

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, String, Text, func
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

    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    root_cause = relationship("Alert", remote_side=[id], foreign_keys=[root_cause_alert_id])
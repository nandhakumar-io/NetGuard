"""Runbook attachments for alerts.

Maps an alert *category* (e.g. "High CPU", "Interface Down", "Temperature
Critical" -- the same free-text value stored on Alert.category) to a
remediation doc/playbook URL, so a new on-call engineer sees exactly what
to do instead of hunting through wikis mid-incident.

Deliberately keyed off category (+ optional source) rather than a FK to
AlertRule, because not every alert originates from a user-defined
AlertRule -- built-in threshold breaches, SNMP traps, and syslog-derived
alerts all set Alert.category too, and all of them should be able to
carry a runbook link.

A runbook can *optionally* carry an actual remediation action on top of
the reference link -- see the remediation_* columns below and
app.services.runbook_execution_service. Deliberately optional/nullable
rather than a separate table: most runbooks are, and should stay, just
docs. Only a runbook someone has explicitly turned into an executable
step gets remediation_enabled=True.
"""
import enum
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base
from app.models.alert import AlertSource
from app.models.user import UserRole


class RemediationActionType(str, enum.Enum):
    # Both action types push a config/command payload to the device over
    # its already-selected protocol (see ProtocolManager.deploy_config) --
    # "restart a service" on network gear is itself almost always a
    # config push (e.g. `restart bgp`, an interface shutdown/no-shutdown
    # pair), so this deliberately reuses the same deploy path deployments
    # and rollbacks already go through rather than adding a second way to
    # write to a device. The two values exist to label *intent* in the UI
    # and audit trail, not to select different code paths.
    RESTART_SERVICE = "restart_service"
    PUSH_CONFIG = "push_config"


class AlertRunbook(Base):
    __tablename__ = "alert_runbooks"
    __table_args__ = (
        UniqueConstraint("category", "source", name="uq_alert_runbook_category_source"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Matched case-insensitively against Alert.category.
    category = Column(String, nullable=False, index=True)
    # NULL = applies to this category regardless of source (SNMP trap,
    # health poll, drift, syslog, protocol failure).
    source = Column(Enum(AlertSource), nullable=True)

    title = Column(String, nullable=False)
    url = Column(String, nullable=False)
    notes = Column(Text, nullable=True)

    # --- Optional remediation step (app.services.runbook_execution_service) ---
    remediation_enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    remediation_action_type = Column(Enum(RemediationActionType), nullable=True)
    remediation_label = Column(String, nullable=True)
    # The command/config text pushed to the device via ProtocolManager.
    # Free text (not templated) deliberately -- keeps this out of the
    # config-template/intent engine entirely; a runbook remediation is a
    # fixed, reviewed-at-authoring-time snippet, not a generated one.
    remediation_command = Column(Text, nullable=True)
    # Minimum role required to trigger this specific remediation (on top
    # of the endpoint's own require_roles(NETWORK_ADMIN) gate) -- lets an
    # admin scope a risky action even tighter than "any admin" if they
    # want it JIT-only in practice (see app.services.jit_service /
    # require_roles' JIT-elevation fallback).
    remediation_required_role = Column(Enum(UserRole), nullable=True)

    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class RunbookExecutionStatus(str, enum.Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class RunbookExecution(Base):
    """Audit trail of every time a runbook's remediation action was
    actually triggered against a device -- separate from the generic
    audit_log (which also gets an entry via ProtocolManager._record) so
    the Alert Runbooks page can show "who ran this, when, against what,
    with what result" without joining through protocol_operations.
    """

    __tablename__ = "runbook_executions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    runbook_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    alert_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    device_id = Column(UUID(as_uuid=True), nullable=False, index=True)

    triggered_by = Column(String, nullable=False)
    status = Column(Enum(RunbookExecutionStatus), nullable=False, default=RunbookExecutionStatus.PENDING)
    output = Column(Text, nullable=True)
    error = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

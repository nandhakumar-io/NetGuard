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
"""
import uuid

from sqlalchemy import Column, DateTime, Enum, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base
from app.models.alert import AlertSource


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

    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

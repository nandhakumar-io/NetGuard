"""Customizable Alert Rules — user-defined threshold conditions that the
SNMP poll sweep evaluates against each device's latest metrics and raises
alerts automatically when breached.

Distinct from the built-in hard-coded thresholds in metrics_service
(which still apply): AlertRules are operator-created, can target specific
device roles/sites/vendors, and are individually toggleable from the UI.
"""
import enum
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class AlertRuleMetric(str, enum.Enum):
    CPU = "cpu"
    MEMORY = "memory"
    BANDWIDTH = "bandwidth"
    TEMPERATURE = "temperature"
    UPTIME = "uptime"
    # Added alongside the cpu/memory/temperature "resource" metrics above --
    # these come from the same per-poll SnmpMetrics (snmp_service.poll_health)
    # but represent link/hardware health rather than utilization, which is
    # exactly the class of real-world condition a NOC actually wants a
    # custom rule for (a flapping port, a dying fan, a failed PSU) instead
    # of only ever alerting on "some percentage got too high".
    INTERFACE_ERRORS = "interface_errors"  # count of interface errors since last poll
    INTERFACE_DOWN_COUNT = "interface_down_count"  # count of admin-up/oper-down interfaces this poll
    FAN_FAILURE = "fan_failure"  # 1 if fan_status == "failed", else 0
    POWER_SUPPLY_FAILURE = "power_supply_failure"  # 1 if power_supply_status == "failed", else 0


class AlertRuleOperator(str, enum.Enum):
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    EQ = "eq"


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)

    metric = Column(Enum(AlertRuleMetric), nullable=False)
    operator = Column(Enum(AlertRuleOperator), nullable=False)
    threshold = Column(Float, nullable=False)

    severity = Column(String, nullable=False, default="warning")  # critical / warning / info

    # Optional scope filters — only evaluate this rule against devices
    # matching these criteria. NULL = all devices.
    scope_vendor = Column(String, nullable=True)
    scope_site = Column(String, nullable=True)
    scope_device_role = Column(String, nullable=True)

    # Minimum seconds between successive firings for the same device,
    # to avoid alert storms from a flapping metric.
    cooldown_seconds = Column(Integer, nullable=False, default=300)

    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    created_by = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

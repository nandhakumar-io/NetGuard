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

    # NOC-specific link/path conditions, on top of the generic resource
    # and interface-count metrics above -- these are the shapes of
    # trouble a network operator actually names a rule after ("alert me
    # if a trunk drops", "alert me if we lose the default route"), not
    # just another percentage. See alert_rule_engine._metric_value for
    # how each is computed and what it costs to evaluate.
    TRUNK_PORT_DOWN = "trunk_port_down"  # count of trunk-mode switchports currently oper-down
    SFP_PORT_DOWN = "sfp_port_down"  # count of down ports on likely SFP/optic-speed interfaces (>=1G)
    ROUTE_UNREACHABLE = "route_unreachable"  # 1 if the device's default route is missing from its routing table
    PING_PACKET_LOSS_PCT = "ping_packet_loss_pct"  # % of ICMP probes lost over a short burst (reachability sweep)


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

    # values_callable is required here: SQLAlchemy's default Enum(cls)
    # persistence keys off the Python member NAME ("CPU", "GT"), not the
    # str value ("cpu", "gt") the member's `.value` mixin suggests. Every
    # other layer of this feature -- AlertRuleCreate/Update/Read schemas,
    # the frontend's <select> options, and alert_rule_engine._metric_value
    # -- sends/reads the lowercase *value* string, so without this the
    # ORM raises `LookupError: 'cpu' is not among the defined enum
    # values` on every single insert/update, i.e. no custom rule (any
    # metric, any operator) could ever be saved. See migration 0094 for
    # the corresponding Postgres enum-label + existing-row backfill.
    metric = Column(Enum(AlertRuleMetric, values_callable=lambda e: [m.value for m in e]), nullable=False)
    operator = Column(Enum(AlertRuleOperator, values_callable=lambda e: [m.value for m in e]), nullable=False)
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

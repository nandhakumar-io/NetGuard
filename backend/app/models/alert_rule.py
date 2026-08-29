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
    ForeignKey,
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

    # Routing-protocol adjacency and LAG health -- the actual root cause
    # behind a lot of what the metrics above can only ever see as
    # "device unreachable" or "some percentage got too high". A WAN
    # edge whose OSPF/BGP adjacency has dropped, or a port-channel that
    # quietly lost half its bundled members, both keep answering SNMP
    # and looking "up" on every resource/interface metric this rule
    # engine already had -- these two close that gap. See
    # snmp_service.walk_ospf_neighbors / walk_bgp_peers /
    # walk_lacp_aggregates for how each is collected, and
    # metrics_service._populate_link_metrics for the same opt-in-only
    # gating trunk_port_down/route_unreachable already use (only walked
    # when an enabled rule actually references the metric).
    OSPF_NEIGHBOR_DOWN = "ospf_neighbor_down"  # count of OSPF neighbors not in the "full" state
    BGP_SESSION_DOWN = "bgp_session_down"  # count of BGP peers not in the "established" state
    LACP_MEMBER_DOWN = "lacp_member_down"  # count of LACP aggregators (port-channels) with fewer bundled members than configured

    # Wireless AP radio-health metrics, evaluated per-AP (not per-device)
    # against WirelessAP rows collected by wireless_service.poll_wireless_controller
    # right after each WLC poll -- see alert_rule_engine.evaluate_ap_rules.
    # An AP can be oper_status "associated" (up, badge green) while still
    # being functionally useless to clients sitting on a saturated or
    # noisy channel, which none of the metrics above can see since they
    # only ever look at the controller Device's own SnmpMetrics, never at
    # the APs it manages. Evaluated as the worse of the 2.4GHz/5GHz radio
    # reading on each AP.
    AP_CHANNEL_UTIL_PCT = "ap_channel_util_pct"  # worst-radio bsnAPIfLoadChannelUtilization, percent
    AP_NOISE_DBM = "ap_noise_dbm"  # worst-radio bsnAPIfNoiseNow, dBm (higher/less-negative = noisier)

    # Flow-based traffic metrics, evaluated per-host (src or dst IP) on a
    # schedule against a rolling window of FlowRecord data rather than
    # per-SNMP-poll SnmpMetrics -- see app.services.flow_service's
    # evaluate_flow_alert_rules and app.tasks.run_flow_alert_sweep_task.
    # SNMP interface counters only ever see aggregate octets in/out; these
    # answer "who" and "since when", which is the actual exfil/compromised
    # -host signal a NOC watches Top Talkers for by hand today. Both are
    # evaluated over a fixed window (settings.FLOW_ALERT_WINDOW_MINUTES,
    # not a per-rule field -- keeps the schema/UI unchanged) rather than a
    # per-poll sample.
    FLOW_TOP_TALKER_BYTES = "flow_top_talker_bytes"  # a host's total bytes (src+dst) over the window
    FLOW_NEW_TALKER = "flow_new_talker"  # 1 if a host crossed rule.threshold bytes this window with ~none in the prior window of equal length, else 0


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

    # Tenant scoping (migration 0095_approval_and_tenant_scoping). NULL =
    # global/MSP-authored rule, visible and evaluated across every tenant
    # -- see app.core.deps.get_tenant_scope and app.api.alert_rules. This
    # column existed in the database since 0095 but was missing from this
    # model, which made every non-MSP request touching AlertRule.tenant_id
    # (e.g. the tenant filter in GET /alert-rules) raise AttributeError.
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True)

    # Inheritance (migration 0097_audit_log_tenant_and_rule_inheritance).
    # When set, this is a tenant-authored rule that explicitly overrides
    # the referenced global (tenant_id IS NULL) rule -- either
    # re-thresholding it (enabled=True, different threshold/operator) or
    # suppressing it entirely for this tenant (enabled=False). Evaluation
    # (app.models.alert_rule_engine.evaluate_rules) prefers a tenant rule
    # over the global rule it points at; a tenant rule with no
    # parent_rule_id is additive alongside whatever global rules apply,
    # same as before this column existed.
    parent_rule_id = Column(UUID(as_uuid=True), ForeignKey("alert_rules.id"), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

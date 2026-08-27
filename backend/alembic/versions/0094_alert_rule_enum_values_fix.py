"""Fix alert_rules.metric/operator enum storage: NAME -> value

Revision ID: 0094
Revises: 0093

app.models.alert_rule.AlertRule.metric/operator were declared as
Column(Enum(AlertRuleMetric)) / Column(Enum(AlertRuleOperator)) with no
`values_callable`. SQLAlchemy's default Enum(cls) persistence keys off
the Python member NAME ("CPU", "GT"), not the str value ("cpu", "gt")
the model's own docstrings, the AlertRuleCreate/Update/Read schemas, the
frontend, and alert_rule_engine._metric_value all assume. Net effect:
the underlying Postgres enum types `alertrulemetric` / `alertruleoperator`
only ever had uppercase-NAME labels (CPU, MEMORY, ..., GT, GTE, ...), so
every create/update of a custom Alert Rule -- which always sends a
lowercase value string -- raised `LookupError` in the ORM before the
statement ever reached the database. This is why custom Alert Rules
never fired: none could actually be saved. Migrations 0062/0089 added
*lowercase* labels for the metrics introduced after those PRs
(interface_errors, trunk_port_down, etc) without noticing the original
five (cpu/memory/bandwidth/temperature/uptime) and the operator enum
were never given their lowercase forms.

This migration:
  1. Adds the missing lowercase labels to both enum types (idempotent
     ADD VALUE IF NOT EXISTS, same autocommit pattern as 0062/0089).
  2. Backfills any rows that made it in with the old uppercase-NAME
     values (possible if a row was inserted directly, e.g. via a seed
     script or fixture, bypassing the ORM's LookupError) to lowercase.
  3. Leaves the old uppercase labels in the Postgres type rather than
     attempting to drop them -- Postgres has no ALTER TYPE ... DROP
     VALUE, same rationale documented in 0062/0089's downgrades.

app.models.alert_rule.AlertRule now declares both columns with
`values_callable=lambda e: [m.value for m in e]` so new rows are always
written/read as the lowercase value going forward.
"""
import sqlalchemy as sa

from alembic import op

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None

_METRIC_VALUES = [
    "cpu", "memory", "bandwidth", "temperature", "uptime",
    "interface_errors", "interface_down_count", "fan_failure", "power_supply_failure",
    "trunk_port_down", "sfp_port_down", "route_unreachable", "ping_packet_loss_pct",
]
_OPERATOR_VALUES = ["gt", "gte", "lt", "lte", "eq"]

# name (as originally stored) -> value (as it should be stored going forward)
_METRIC_NAME_TO_VALUE = {
    "CPU": "cpu", "MEMORY": "memory", "BANDWIDTH": "bandwidth",
    "TEMPERATURE": "temperature", "UPTIME": "uptime",
    "INTERFACE_ERRORS": "interface_errors", "INTERFACE_DOWN_COUNT": "interface_down_count",
    "FAN_FAILURE": "fan_failure", "POWER_SUPPLY_FAILURE": "power_supply_failure",
    "TRUNK_PORT_DOWN": "trunk_port_down", "SFP_PORT_DOWN": "sfp_port_down",
    "ROUTE_UNREACHABLE": "route_unreachable", "PING_PACKET_LOSS_PCT": "ping_packet_loss_pct",
}
_OPERATOR_NAME_TO_VALUE = {"GT": "gt", "GTE": "gte", "LT": "lt", "LTE": "lte", "EQ": "eq"}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # sqlite/test DBs use a plain VARCHAR check, not a native enum type

    with op.get_context().autocommit_block():
        for value in _METRIC_VALUES:
            op.execute(f"ALTER TYPE alertrulemetric ADD VALUE IF NOT EXISTS '{value}'")
        for value in _OPERATOR_VALUES:
            op.execute(f"ALTER TYPE alertruleoperator ADD VALUE IF NOT EXISTS '{value}'")

    for name, value in _METRIC_NAME_TO_VALUE.items():
        bind.execute(
            sa.text("UPDATE alert_rules SET metric = :value::alertrulemetric WHERE metric::varchar = :name"),
            {"value": value, "name": name},
        )
    for name, value in _OPERATOR_NAME_TO_VALUE.items():
        bind.execute(
            sa.text("UPDATE alert_rules SET operator = :value::alertruleoperator WHERE operator::varchar = :name"),
            {"value": value, "name": name},
        )


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE, and reverting the
    # lowercased rows back to uppercase NAMEs would just reintroduce the
    # bug this migration fixes -- left as a no-op, same rationale as
    # 0062/0089's downgrades.
    pass

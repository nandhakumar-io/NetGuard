"""Expand AlertRuleMetric with interface/hardware health metrics

Revision ID: 0062
Revises: 0061
Create Date: 2026-08-14

app.models.alert_rule.AlertRuleMetric gains four new members --
interface_errors, interface_down_count, fan_failure, power_supply_failure
-- alongside the existing cpu/memory/bandwidth/temperature/uptime, so
custom Alert Rules can key off the same link/hardware-health signals
snmp_service.evaluate_thresholds() already watches, instead of only ever
resource-utilization percentages. See app.services.alert_rule_engine for
the evaluator that reads these off SnmpMetrics.

Postgres enum types can't have a value removed or reordered without a
full rebuild, but ADD VALUE is safe and cheap; this only adds new
members. Note ADD VALUE cannot run inside a transaction block in older
Postgres versions, hence autocommit here (same pattern as 0026).
"""

from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None

_NEW_VALUES = ["interface_errors", "interface_down_count", "fan_failure", "power_supply_failure"]


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in _NEW_VALUES:
            op.execute(f"ALTER TYPE alertrulemetric ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE -- removing these would
    # require rebuilding the enum type and rewriting any rows using them
    # first. Left as a no-op since the added values are harmless to leave
    # in place (same rationale as 0026_healthcolor_grey).
    pass

"""Expand AlertRuleMetric with trunk/SFP/route/ping-loss link metrics

Revision ID: 0089
Revises: 0088
Create Date: 2026-08-21

app.models.alert_rule.AlertRuleMetric gains four new members --
trunk_port_down, sfp_port_down, route_unreachable, ping_packet_loss_pct --
so custom Alert Rules can key off the specific link/path conditions a NOC
actually asks for by name (a trunk dropping, an optic going dark, the
default route disappearing, sustained ping loss to a device) instead of
only generic resource-utilization or interface-count metrics. See
app.services.alert_rule_engine for the evaluator that computes these.

Same ADD VALUE / autocommit pattern as 0062 (which did this same kind of
expansion) -- Postgres enum types can't have a value removed or reordered
without a full rebuild, but ADD VALUE is safe and cheap, and ADD VALUE
cannot run inside a transaction block in older Postgres versions.
"""

from alembic import op

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None

_NEW_VALUES = ["trunk_port_down", "sfp_port_down", "route_unreachable", "ping_packet_loss_pct"]


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in _NEW_VALUES:
            op.execute(f"ALTER TYPE alertrulemetric ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE -- see 0062's downgrade
    # for the same rationale. Left as a no-op; harmless to leave in place.
    pass

"""Expand AlertRuleMetric with flow-based traffic metrics

Revision ID: 0111
Revises: 0110
Create Date: 2026-08-29

app.models.alert_rule.AlertRuleMetric gains two new members --
flow_top_talker_bytes, flow_new_talker -- so custom Alert Rules can key
off NetFlow/IPFIX/sFlow traffic data (app.services.flow_service) instead
of only ever needing someone to look at the Traffic Analysis page by
hand. A host suddenly moving a lot of bytes, or a host that's never
talked before suddenly showing up as a top talker, is a classic
exfil/compromised-host signal that SNMP interface counters (aggregate
octets only) can't distinguish on their own. See
flow_service.evaluate_flow_alert_rules for the evaluator and
app.tasks.run_flow_alert_sweep_task for the Celery beat entry point.

Same ADD VALUE / autocommit pattern as 0089/0062/0109 -- Postgres enum
types can't have a value removed or reordered without a full rebuild,
but ADD VALUE is safe and cheap, and ADD VALUE cannot run inside a
transaction block in older Postgres versions.
"""

from alembic import op

revision = "0111"
down_revision = "0110"
branch_labels = None
depends_on = None

_NEW_VALUES = ["flow_top_talker_bytes", "flow_new_talker"]


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in _NEW_VALUES:
            op.execute(f"ALTER TYPE alertrulemetric ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE -- see 0062/0089/0109's
    # downgrade for the same rationale. Left as a no-op; harmless to
    # leave in place.
    pass

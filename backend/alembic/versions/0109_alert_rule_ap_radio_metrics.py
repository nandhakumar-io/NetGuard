"""Expand AlertRuleMetric with per-AP radio-health metrics

Revision ID: 0109
Revises: 0108
Create Date: 2026-08-29

app.models.alert_rule.AlertRuleMetric gains two new members --
ap_channel_util_pct, ap_noise_dbm -- so custom Alert Rules can key off
per-radio wireless AP telemetry (WirelessAP.channel_util_2g/5g,
noise_2g/5g) already collected by wireless_service.poll_wireless_controller
but never evaluated against anything: an AP can show "associated" (up)
while a saturated or noisy channel still makes it useless to clients.
See app.services.alert_rule_engine.evaluate_ap_rules for the evaluator.

Same ADD VALUE / autocommit pattern as 0089/0062 -- Postgres enum types
can't have a value removed or reordered without a full rebuild, but ADD
VALUE is safe and cheap, and ADD VALUE cannot run inside a transaction
block in older Postgres versions.
"""

from alembic import op

revision = "0109"
down_revision = "0108"
branch_labels = None
depends_on = None

_NEW_VALUES = ["ap_channel_util_pct", "ap_noise_dbm"]


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in _NEW_VALUES:
            op.execute(f"ALTER TYPE alertrulemetric ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE -- see 0062/0089's
    # downgrade for the same rationale. Left as a no-op; harmless to
    # leave in place.
    pass

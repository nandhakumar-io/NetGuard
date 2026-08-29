"""Expand AlertRuleMetric with routing-protocol and LACP health metrics

Revision ID: 0112
Revises: 0111
Create Date: 2026-08-29

app.models.alert_rule.AlertRuleMetric gains three new members --
ospf_neighbor_down, bgp_session_down, lacp_member_down -- so custom Alert
Rules can key off routing-protocol adjacency (OSPF/BGP, via the new
snmp_service.walk_ospf_neighbors / walk_bgp_peers) and LACP port-channel
bundling health (snmp_service.walk_lacp_aggregates) instead of only ever
seeing these failures downstream as a generic "device unreachable" or a
link that stays "up" while quietly running at reduced capacity.

Same ADD VALUE / autocommit pattern as 0062/0089/0109/0111 -- Postgres
enum types can't have a value removed or reordered without a full
rebuild, but ADD VALUE is safe and cheap, and ADD VALUE cannot run
inside a transaction block in older Postgres versions.
"""

from alembic import op

revision = "0112"
down_revision = "0111"
branch_labels = None
depends_on = None

_NEW_VALUES = ["ospf_neighbor_down", "bgp_session_down", "lacp_member_down"]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # sqlite/test DBs use a plain VARCHAR check, not a native enum type

    with op.get_context().autocommit_block():
        for value in _NEW_VALUES:
            op.execute(f"ALTER TYPE alertrulemetric ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE -- see 0062/0089/0109/0111's
    # downgrade for the same rationale. Left as a no-op; harmless to
    # leave in place.
    pass

"""Discovered neighbor switchport (trunk/access mode + VLAN) columns

Revision ID: 0077
Revises: 0076
Create Date: 2026-08-19 00:00:00.000000

The Discovery page's LLDP/CDP tables (and the Topology page link
tooltips fed by DiscoveredNeighbor) could only ever show *that* a link
exists, never whether the local port is trunking and which VLAN(s) ride
it -- app.api.devices._persist_discovered_neighbors now resolves this
via the same Junos-config / Q-BRIDGE-MIB lookup the device Interfaces
tab already uses (see snmp_service.walk_switchport_vlans /
config_format_service.parse_junos_switchport_config), so it needs
somewhere to persist alongside the rest of the neighbor row.

trunk_vlans is stored as a JSON-encoded string (comma-separated VLAN
IDs) rather than a native array column -- this table has no other
array-typed columns and a plain Text column keeps the migration
portable without pulling in a Postgres-specific ARRAY type for a
handful of small ID lists that are only ever displayed, never queried.
"""
import sqlalchemy as sa

from alembic import op
from migration_helpers import add_column_if_missing

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("discovered_neighbors", sa.Column("port_mode", sa.String(length=16), nullable=True))
    add_column_if_missing("discovered_neighbors", sa.Column("vlan", sa.String(length=32), nullable=True))
    add_column_if_missing("discovered_neighbors", sa.Column("trunk_vlans", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("discovered_neighbors", "trunk_vlans")
    op.drop_column("discovered_neighbors", "vlan")
    op.drop_column("discovered_neighbors", "port_mode")

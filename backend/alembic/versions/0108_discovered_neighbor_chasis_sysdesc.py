"""discovered_neighbors: neighbor_chassis_id + neighbor_sys_desc

Revision ID: 0108
Revises: 0107
Create Date: 2026-08-29

Backs two new Wireless-page features:
  - AP -> switchport correlation ("this AP is on Switch3 Gi1/0/24"),
    which joins WirelessAP.mac_address against neighbor_chassis_id
    (the raw LLDP chassis-id, which for the common macAddress(4)
    subtype already *is* the neighbor's MAC).
  - Unregistered/rogue-ish AP detection, which pattern-matches known
    AP vendor strings against neighbor_sys_desc for switchports that
    look like an AP but have no corresponding WirelessAP row.

Both columns were already computed in snmp_service._discover_lldp_neighbors
(chassis_id) or trivial to add (sys_desc was walked into LLDP_OIDS but
never actually fetched) -- this just gives them somewhere to land.
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing

revision = "0108"
down_revision = "0107"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("discovered_neighbors", sa.Column("neighbor_chassis_id", sa.String(length=64), nullable=True))
    add_column_if_missing("discovered_neighbors", sa.Column("neighbor_sys_desc", sa.Text(), nullable=True))
    from migration_helpers import create_index_if_missing
    create_index_if_missing(
        "ix_discovered_neighbors_neighbor_chassis_id", "discovered_neighbors", ["neighbor_chassis_id"]
    )


def downgrade() -> None:
    from migration_helpers import drop_column_if_exists, drop_index_if_exists
    drop_index_if_exists("ix_discovered_neighbors_neighbor_chassis_id", "discovered_neighbors")
    drop_column_if_exists("discovered_neighbors", "neighbor_sys_desc")
    drop_column_if_exists("discovered_neighbors", "neighbor_chassis_id")

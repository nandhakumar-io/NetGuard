"""Discovered host IPAM cross-reference: ipam_status, ipam_reservation_note

Revision ID: 0075
Revises: 0074
Create Date: 2026-08-19 00:00:00.000000

Adds the columns behind app.services.network_discovery_service's IPAM
dedup check -- distinguishing a responsive discovered host that IPAM
already has a RESERVED IPReservation for ("expected", just not
provisioned yet) from one on a managed subnet with no reservation at all
("rogue"). See app.models.network_discovery.DiscoveredHostIpamStatus.
"""
import sqlalchemy as sa

from alembic import op
from migration_helpers import add_column_if_missing

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "discovered_hosts",
        sa.Column("ipam_status", sa.String(), nullable=False, server_default="unmanaged"),
    )
    add_column_if_missing(
        "discovered_hosts",
        sa.Column("ipam_reservation_note", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("discovered_hosts", "ipam_reservation_note")
    op.drop_column("discovered_hosts", "ipam_status")

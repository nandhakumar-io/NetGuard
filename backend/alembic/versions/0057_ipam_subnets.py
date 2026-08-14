"""IPAM: subnets + ip_reservations tables

Revision ID: 0057
Revises: 0056
Create Date: 2026-08-14 00:00:00.000000

Adds the IPAM feature: a `subnets` table (CIDR blocks with VLAN/site
metadata) and an `ip_reservations` table for addresses held out of the
free pool for a reason other than an active device sitting on them
(reserved/gateway/broadcast/network -- see
app.models.subnet.IPAddressState). Assigned addresses are intentionally
*not* stored here; they're derived live from devices.ip_address (see
app.services.ipam_service), so this migration adds no new column to
`devices` and no backfill is needed.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from migration_helpers import create_index_if_missing, create_table_if_missing

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_table_if_missing(
        "subnets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("cidr", sa.String(), nullable=False, unique=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("vlan_id", sa.Integer(), nullable=True),
        sa.Column("site", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tags", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    create_index_if_missing("ix_subnets_cidr", "subnets", ["cidr"], unique=True)
    create_index_if_missing("ix_subnets_vlan_id", "subnets", ["vlan_id"])
    create_index_if_missing("ix_subnets_site", "subnets", ["site"])

    create_table_if_missing(
        "ip_reservations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("subnet_id", UUID(as_uuid=True), sa.ForeignKey("subnets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ip_address", sa.String(), nullable=False),
        sa.Column(
            "state",
            sa.Enum("assigned", "reserved", "gateway", "broadcast", "network", name="ipaddressstate"),
            nullable=False,
            server_default="reserved",
        ),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("subnet_id", "ip_address", name="uq_ip_reservations_subnet_ip"),
    )
    create_index_if_missing("ix_ip_reservations_subnet_id", "ip_reservations", ["subnet_id"])
    create_index_if_missing("ix_ip_reservations_ip_address", "ip_reservations", ["ip_address"])


def downgrade() -> None:
    from alembic import op

    op.drop_table("ip_reservations")
    op.drop_table("subnets")
    sa.Enum(name="ipaddressstate").drop(op.get_bind(), checkfirst=True)

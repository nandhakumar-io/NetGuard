"""IPAM: nmap scan results (subnet_scanned_hosts + subnets.last_scanned_at)

Revision ID: 0061
Revises: 0060
Create Date: 2026-08-14 00:00:00.000000

Adds live ping-sweep scan results as a fourth "used" signal for IPAM
utilization, alongside assigned/interface/reserved (see
app.services.ipam_service.scan_subnet's docstring) -- catches unmanaged
hosts (PCs, printers, phones) that hold an address in a subnet but were
never entered as a Device and have no config for NetGuard to parse.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from migration_helpers import (
    add_column_if_missing,
    create_index_if_missing,
    create_table_if_missing,
)

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("subnets", sa.Column("last_scanned_at", sa.DateTime(timezone=True), nullable=True))

    create_table_if_missing(
        "subnet_scanned_hosts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("subnet_id", UUID(as_uuid=True), sa.ForeignKey("subnets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ip_address", sa.String(), nullable=False),
        sa.Column("hostname", sa.String(), nullable=True),
        sa.Column("scanned_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("subnet_id", "ip_address", name="uq_subnet_scanned_hosts_subnet_ip"),
    )
    create_index_if_missing("ix_subnet_scanned_hosts_subnet_id", "subnet_scanned_hosts", ["subnet_id"])
    create_index_if_missing("ix_subnet_scanned_hosts_ip_address", "subnet_scanned_hosts", ["ip_address"])


def downgrade() -> None:
    from alembic import op

    op.drop_table("subnet_scanned_hosts")
    op.drop_column("subnets", "last_scanned_at")

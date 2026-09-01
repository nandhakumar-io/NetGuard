"""device snmp_stack_aware flag

Revision ID: 0116
Revises: 0115
Create Date: 2026-08-29

Adds Device.snmp_stack_aware -- lets a stacked Cisco/Arista switch opt
into taking the WORST (max) CPU/memory reading across stack members
instead of the lowest-index row, which is what snmp_service.poll_health
used unconditionally before this (see test_cisco_uses_lowest_table_index_directly
for why that's still the right default for the common single-chassis
case). Default false so every existing device's behavior is unchanged
until explicitly opted in.
"""
import sqlalchemy as sa

from alembic import op
from migration_helpers import add_column_if_missing

revision = "0116"
down_revision = "0115"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "devices",
        sa.Column("snmp_stack_aware", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("devices", "snmp_stack_aware")

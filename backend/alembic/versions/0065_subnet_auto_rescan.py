"""IPAM: scheduled subnet re-scan fields

Revision ID: 0065
Revises: 0064
Create Date: 2026-08-14 00:00:00.000000

Adds Subnet.auto_rescan_enabled / rescan_interval_hours so
app.tasks.run_subnet_rescan_sweep_task can re-run scan_subnet()
automatically on its own cadence instead of only ever firing from a
manual click on the IPAM page. See app.services.ipam_service.due_for_rescan.
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "subnets",
        sa.Column("auto_rescan_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    add_column_if_missing(
        "subnets",
        sa.Column("rescan_interval_hours", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    from alembic import op

    op.drop_column("subnets", "rescan_interval_hours")
    op.drop_column("subnets", "auto_rescan_enabled")

"""add flow bandwidth columns to path hops

Revision ID: 0103
Revises: 0102
Create Date: 2026-08-28

Lets a path trace show live NetFlow/sFlow bandwidth alongside each hop's
reachability data (app.services.flow_service.recent_bandwidth_for_device),
so a degraded hop can be cross-checked against "is this hop actually
congested" instead of just "did it answer".
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing, drop_column_if_exists

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("path_hops", sa.Column("flow_bytes_per_sec", sa.Float(), nullable=True))
    add_column_if_missing("path_hops", sa.Column("flow_top_protocol", sa.String(), nullable=True))


def downgrade() -> None:
    drop_column_if_exists("path_hops", "flow_top_protocol")
    drop_column_if_exists("path_hops", "flow_bytes_per_sec")

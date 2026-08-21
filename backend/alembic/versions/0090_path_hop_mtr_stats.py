"""add mtr stat columns to path hops

Revision ID: 0090
Revises: 0089
Create Date: 2026-08-21
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing, drop_column_if_exists

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("path_hops", sa.Column("sent", sa.Integer(), nullable=True))
    add_column_if_missing("path_hops", sa.Column("last_rtt_ms", sa.Float(), nullable=True))
    add_column_if_missing("path_hops", sa.Column("best_rtt_ms", sa.Float(), nullable=True))
    add_column_if_missing("path_hops", sa.Column("worst_rtt_ms", sa.Float(), nullable=True))
    add_column_if_missing("path_hops", sa.Column("stddev_rtt_ms", sa.Float(), nullable=True))


def downgrade() -> None:
    drop_column_if_exists("path_hops", "stddev_rtt_ms")
    drop_column_if_exists("path_hops", "worst_rtt_ms")
    drop_column_if_exists("path_hops", "best_rtt_ms")
    drop_column_if_exists("path_hops", "last_rtt_ms")
    drop_column_if_exists("path_hops", "sent")

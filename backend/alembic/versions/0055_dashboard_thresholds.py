"""dashboard preference alert thresholds

Revision ID: 0055
Revises: 0054
Create Date: 2026-08-13 00:00:00.000000

Adds `dashboard_preferences.thresholds` -- a per-user JSON blob of
warn/critical bands for the dashboard's CPU/RAM/bandwidth gauges and
Top-N widgets, so an admin can decide what "high" means for their own
fleet instead of the hardcoded bands every user previously got.
"""
import sqlalchemy as sa

from alembic import op
from migration_helpers import add_column_if_missing

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "dashboard_preferences",
        sa.Column("thresholds", sa.Text(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("dashboard_preferences", "thresholds")

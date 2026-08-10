"""dashboard_preferences

Revision ID: 0043
Revises: 0042
Create Date: 2026-08-10

Adds dashboard_preferences: one row per user storing their chosen
dashboard widget selection/order (see app.services.dashboard_widgets and
GET/PUT /dashboard/preferences). The canonical widget catalog lives in
code, not the DB -- this table only stores each user's override of the
registry defaults.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import create_index_if_missing, create_table_if_missing

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_table_if_missing(
        "dashboard_preferences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False, unique=True),
        sa.Column("layout", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    create_index_if_missing("ix_dashboard_preferences_user_id", "dashboard_preferences", ["user_id"])


def downgrade() -> None:
    op.drop_table("dashboard_preferences")

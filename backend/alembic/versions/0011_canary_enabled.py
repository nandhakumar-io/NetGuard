"""Canary multi-device deploy flag on change_requests (SRS 6.6)

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-01

Adds canary_enabled: when true and the CR targets more than one device,
the first target device deploys and clears its health-monitoring window
before the rest of the fleet is dispatched at all (see
app.tasks.run_deployment_pipeline_task / canary_gate_task).
"""
import sqlalchemy as sa
from alembic import op

from migration_helpers import add_column_if_missing

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "change_requests",
        sa.Column("canary_enabled", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("change_requests", "canary_enabled")
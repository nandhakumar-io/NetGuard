"""Drift: tag detections that happened during an active maintenance window

Revision ID: 0066
Revises: 0065
Create Date: 2026-08-15 00:00:00.000000

Adds ConfigDrift.maintenance_window_id so a drift row that fires while its
device sits inside a planned MaintenanceWindow can be shown as "expected --
device in maintenance" on the Drift page, instead of looking identical to
an unplanned change. The alert raised alongside it was already suppressed
via Alert.suppressed_by_window_id (see alert_service.raise_alert) -- this
column gives the underlying ConfigDrift row the same context, since the
drift itself is still recorded (and still worth reviewing) even though it
shouldn't page anyone. See app.services.drift_service.detect_drift.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from migration_helpers import add_column_if_missing

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "config_drifts",
        sa.Column(
            "maintenance_window_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("maintenance_windows.id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    from alembic import op

    op.drop_column("config_drifts", "maintenance_window_id")

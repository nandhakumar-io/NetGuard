"""add alert_snoozes table + alerts.muted_by_snooze_id

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-06

Per-device or per-rule alert snooze/mute with a mandatory expiry (see
app.models.alert_snooze.AlertSnooze docstring for how this differs from
maintenance-window suppression and topology-based correlation, the two
other ways an alert can already be marked "not currently urgent").
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "alert_snoozes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_alert_snoozes_device_id", "alert_snoozes", ["device_id"])
    op.create_index("ix_alert_snoozes_category", "alert_snoozes", ["category"])
    op.create_index("ix_alert_snoozes_expires_at", "alert_snoozes", ["expires_at"])

    op.add_column("alerts", sa.Column("muted_by_snooze_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("alert_snoozes.id"), nullable=True))
    op.create_index("ix_alerts_muted_by_snooze_id", "alerts", ["muted_by_snooze_id"])


def downgrade():
    op.drop_index("ix_alerts_muted_by_snooze_id", table_name="alerts")
    op.drop_column("alerts", "muted_by_snooze_id")

    op.drop_index("ix_alert_snoozes_expires_at", table_name="alert_snoozes")
    op.drop_index("ix_alert_snoozes_category", table_name="alert_snoozes")
    op.drop_index("ix_alert_snoozes_device_id", table_name="alert_snoozes")
    op.drop_table("alert_snoozes")

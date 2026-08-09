"""device status history (fleet availability / flap tracking)

Revision ID: 0039
Revises: 0038
Create Date: 2026-08-09

Adds:
  - device_status_history table: one row per Device.status transition
    (see app.models.device_status_history.DeviceStatusHistory). Written by
    app.services.reachability_service.check_device and
    app.services.metrics_service.poll_device whenever the status they're
    about to set differs from the device's current status. Powers the
    fleet availability % rollup and the "unstable devices" (flapping)
    widget -- neither was previously computable since Device.status was a
    single live column with no transition history behind it.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import create_index_if_missing, create_table_if_missing

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

_STATUS_ENUM = postgresql.ENUM("online", "offline", "degraded", "unknown", name="devicestatus", create_type=False)


def upgrade():
    create_table_if_missing(
        "device_status_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=False),
        sa.Column("status", _STATUS_ENUM, nullable=False),
        sa.Column("previous_status", _STATUS_ENUM, nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    create_index_if_missing(
        "ix_device_status_history_device_id", "device_status_history", ["device_id"]
    )
    create_index_if_missing(
        "ix_device_status_history_changed_at", "device_status_history", ["changed_at"]
    )


def downgrade():
    op.drop_index("ix_device_status_history_changed_at", table_name="device_status_history")
    op.drop_index("ix_device_status_history_device_id", table_name="device_status_history")
    op.drop_table("device_status_history")

"""add maintenance windows, alert maintenance-suppression link, firmware upgrade orchestration

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-05

Two SRS gaps closed here:

1. Maintenance windows for alert suppression. Change requests already had
   maintenance_window_start/end, but that's scoped to one change and one
   deployment -- there was no general "silence alerts for this
   device/site while we work on it" mechanism independent of a specific
   change request. Adds `maintenance_windows` plus `alerts.suppressed_by_window_id`
   so app.services.alert_service can mark an alert raised while inside an
   active window as suppressed (still stored/auditable, just not paged).

2. Firmware/OS upgrade orchestration. EOL tracking (eol_service) could
   tell you a device was out of support but nothing could act on it.
   Adds `firmware_upgrades` to drive devices through
   download -> install -> reboot -> verify (with automatic rollback on a
   failed post-reboot check), individually or as a batch.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import (
    add_column_if_missing,
    create_index_if_missing,
    create_table_if_missing,
    drop_column_if_exists,
    drop_index_if_exists,
)

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade():
    # --- maintenance_windows ---
    create_table_if_missing(
        "maintenance_windows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "scope",
            sa.Enum("device", "site", "fleet", name="maintenancescope"),
            nullable=False,
            server_default="device",
        ),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=True),
        sa.Column("site", sa.String(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    create_index_if_missing("ix_maintenance_windows_device_id", "maintenance_windows", ["device_id"])
    create_index_if_missing("ix_maintenance_windows_site", "maintenance_windows", ["site"])
    create_index_if_missing("ix_maintenance_windows_starts_ends", "maintenance_windows", ["starts_at", "ends_at"])

    # --- alerts.suppressed_by_window_id ---
    # Distinct from the existing `suppressed` boolean (topology-correlation
    # consequence-of-another-alert) and existing `suppressed` column reuse
    # would conflate two different reasons an alert is downranked, so this
    # is its own nullable FK: null means "not suppressed by a maintenance
    # window", set means "raised while this window was active".
    add_column_if_missing(
        "alerts",
        sa.Column("suppressed_by_window_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("maintenance_windows.id"), nullable=True),
    )
    create_index_if_missing("ix_alerts_suppressed_by_window_id", "alerts", ["suppressed_by_window_id"])

    # --- firmware_upgrades ---
    create_table_if_missing(
        "firmware_upgrades",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=False),
        sa.Column("from_version", sa.String(), nullable=True),
        sa.Column("target_version", sa.String(), nullable=False),
        sa.Column("image_filename", sa.String(), nullable=False),
        sa.Column("image_sha256", sa.String(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "scheduled", "downloading", "installing", "rebooting",
                "verifying", "completed", "failed", "rolled_back", "cancelled",
                name="firmwareupgradestatus",
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("current_step_detail", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("maintenance_window_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("maintenance_windows.id"), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pre_upgrade_snapshot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("config_snapshots.id"), nullable=True),
        sa.Column("reboot_wait_seconds", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("initiated_by", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    create_index_if_missing("ix_firmware_upgrades_batch_id", "firmware_upgrades", ["batch_id"])
    create_index_if_missing("ix_firmware_upgrades_device_id", "firmware_upgrades", ["device_id"])
    create_index_if_missing("ix_firmware_upgrades_created_at", "firmware_upgrades", ["created_at"])


def downgrade():
    op.drop_index("ix_firmware_upgrades_created_at", table_name="firmware_upgrades")
    op.drop_index("ix_firmware_upgrades_device_id", table_name="firmware_upgrades")
    op.drop_index("ix_firmware_upgrades_batch_id", table_name="firmware_upgrades")
    op.drop_table("firmware_upgrades")
    sa.Enum(name="firmwareupgradestatus").drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_alerts_suppressed_by_window_id", table_name="alerts")
    op.drop_column("alerts", "suppressed_by_window_id")

    op.drop_index("ix_maintenance_windows_starts_ends", table_name="maintenance_windows")
    op.drop_index("ix_maintenance_windows_site", table_name="maintenance_windows")
    op.drop_index("ix_maintenance_windows_device_id", table_name="maintenance_windows")
    op.drop_table("maintenance_windows")
    sa.Enum(name="maintenancescope").drop(op.get_bind(), checkfirst=True)

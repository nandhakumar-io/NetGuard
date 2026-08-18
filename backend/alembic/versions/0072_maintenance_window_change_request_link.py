"""maintenance_window_change_request_link

Adds maintenance_windows.change_request_id -- lets app.services.
maintenance_window_service auto-create (and later auto-cancel) a
MaintenanceWindow from a ChangeRequest's declared maintenance_window_
start/end instead of those two columns being informational-only.

Before this, a ChangeRequest carried its own maintenance_window_start/
end (used only to gate *when deployment fires*, see api.change_requests.
approve_change_request) that never touched app.models.maintenance_
window.MaintenanceWindow -- the table alert_service actually checks to
decide whether to suppress an alert. A device being deployed to during
its own approved change's declared window still paged NOC like any
other unplanned event; the two "maintenance window" concepts existed
in the schema but were never connected.

Nullable and indexed, same shape as the existing recurrence_id link to
RecurringMaintenanceSchedule -- NULL for windows created directly via
POST /maintenance-windows, set for windows this migration's new
change-request auto-suppression path creates.

Revision ID: 0072
Revises: 0071
Create Date: 2026-08-18 00:00:00.000000
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from migration_helpers import (
    add_column_if_missing,
    create_index_if_missing,
    drop_column_if_exists,
    drop_index_if_exists,
)

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "maintenance_windows",
        sa.Column("change_request_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("change_requests.id"), nullable=True),
    )
    create_index_if_missing(
        "ix_maintenance_windows_change_request_id", "maintenance_windows", ["change_request_id"]
    )


def downgrade() -> None:
    drop_index_if_exists("ix_maintenance_windows_change_request_id", "maintenance_windows")
    drop_column_if_exists("maintenance_windows", "change_request_id")

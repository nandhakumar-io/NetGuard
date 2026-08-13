"""Add recurring_maintenance_schedules table and maintenance_windows.recurrence_id

Revision ID: 0054
Revises: 0053
Create Date: 2026-08-13

Scheduled/recurring change windows -- patch Tuesdays, monthly firmware
windows -- defined once via RecurringMaintenanceSchedule and materialized
into ordinary MaintenanceWindow rows by
app.services.recurring_window_service, tagged via the new
maintenance_windows.recurrence_id column.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None

recurrence_frequency_enum = postgresql.ENUM(
    "weekly", "monthly", name="recurrencefrequency", create_type=True,
)


def upgrade() -> None:
    recurrence_frequency_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "recurring_maintenance_schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("scope", sa.String(), nullable=False, server_default="device"),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=True, index=True),
        sa.Column("site", sa.String(), nullable=True, index=True),
        sa.Column("frequency", recurrence_frequency_enum, nullable=False),
        sa.Column("interval", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("week_of_month", sa.Integer(), nullable=True),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("active_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.add_column(
        "maintenance_windows",
        sa.Column(
            "recurrence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("recurring_maintenance_schedules.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_maintenance_windows_recurrence_id", "maintenance_windows", ["recurrence_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_maintenance_windows_recurrence_id", table_name="maintenance_windows")
    op.drop_column("maintenance_windows", "recurrence_id")
    op.drop_table("recurring_maintenance_schedules")
    recurrence_frequency_enum.drop(op.get_bind(), checkfirst=True)

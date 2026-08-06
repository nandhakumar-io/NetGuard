"""In-app notification center (FR-11)

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-31

Adds the `notifications` table backing the WebSocket-driven notification
center: one row per notification_service.notify() call (deploy
success/fail, rollback, drift), independent of the existing `alerts` table.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import create_index_if_missing, create_table_if_missing

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

notification_event_type = postgresql.ENUM(
    "deployment_succeeded",
    "deployment_failed",
    "rollback_triggered",
    "drift_high",
    "drift_critical",
    "generic",
    name="notificationeventtype",
)

notification_severity = postgresql.ENUM(
    "info",
    "warning",
    "critical",
    name="notificationseverity",
)


def upgrade() -> None:
    bind = op.get_bind()
    notification_event_type.create(bind, checkfirst=True)
    notification_severity.create(bind, checkfirst=True)

    create_table_if_missing(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_type", notification_event_type, nullable=False, server_default="generic"),
        sa.Column("severity", notification_severity, nullable=False, server_default="info"),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("device_hostname", sa.String(), nullable=True),
        sa.Column("change_request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("deployment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("read", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    create_index_if_missing("ix_notifications_created_at", "notifications", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_notifications_created_at", table_name="notifications")
    op.drop_table("notifications")
    notification_severity.drop(op.get_bind(), checkfirst=True)
    notification_event_type.drop(op.get_bind(), checkfirst=True)

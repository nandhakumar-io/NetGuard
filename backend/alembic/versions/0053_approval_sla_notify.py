"""Add change_requests.sla_last_notified_stage for Slack/Teams SLA reminders

Revision ID: 0053
Revises: 0052
Create Date: 2026-08-13

Tracks which approval-SLA reminder stage ("due_soon" / "overdue") has
already been posted to Slack/Teams for a change request, so the periodic
sweep (app.tasks.run_approval_sla_notify_sweep_task) never re-posts the
same stage twice. See app.services.approval_sla_notifier_service.
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing, drop_column_if_exists

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "change_requests", sa.Column("sla_last_notified_stage", sa.String(), nullable=True)
    )


def downgrade() -> None:
    drop_column_if_exists("change_requests", "sla_last_notified_stage")

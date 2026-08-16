"""User Management: last_login_at; Database Backups: backup_jobs table

Revision ID: 0069
Revises: 0068
Create Date: 2026-08-16 00:00:00.000000

Two independent additions bundled into one migration since neither is
large enough to warrant its own revision:

  1. users.last_login_at -- stamped by app.api.auth._issue_token_pair
     (every login path funnels through it) -- backs the new User
     Management page's Last Login column (app.api.user_management).
  2. backup_jobs -- one row per on-demand or scheduled database backup
     (pg_dump run), tracking status/size/file path/error so the new
     Backups page has real history instead of only a "click to run"
     button with no record of what happened last time. See
     app.services.backup_service / app.api.backups.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from migration_helpers import (
    add_column_if_missing,
    create_index_if_missing,
    create_table_if_missing,
)

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "users",
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )

    create_table_if_missing(
        "backup_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("status", sa.String(), nullable=False, server_default="running"),
        sa.Column("file_path", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("triggered_by", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
    )
    create_index_if_missing("ix_backup_jobs_started_at", "backup_jobs", ["started_at"])


def downgrade() -> None:
    from alembic import op

    op.drop_index("ix_backup_jobs_started_at", table_name="backup_jobs")
    op.drop_table("backup_jobs")
    op.drop_column("users", "last_login_at")

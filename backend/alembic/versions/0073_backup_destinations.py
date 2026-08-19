"""Cloud Backups: backup_destinations table, backup_jobs.offsite_results

Revision ID: 0073
Revises: 0072
Create Date: 2026-08-19 00:00:00.000000

Adds off-site backup destinations (AWS S3, Azure Blob Storage, or a plain
remote server over SFTP) that completed database backups get pushed to,
on top of the existing local-disk storage (BACKUP_STORAGE_DIR). See
app.models.backup_destination and app.services.backup_destination_service
for the storage model and upload logic, and
app.services.backup_service._upload_to_destinations for where uploads are
triggered.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from migration_helpers import (
    add_column_if_missing,
    create_table_if_missing,
)

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_table_if_missing(
        "backup_destinations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("config_encrypted", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_status", sa.String(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
    )

    add_column_if_missing(
        "backup_jobs",
        sa.Column("offsite_results", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    from alembic import op

    op.drop_column("backup_jobs", "offsite_results")
    op.drop_table("backup_destinations")

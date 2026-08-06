"""Alert dedup columns: last_seen_at + occurrence_count

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-02

Backs app.services.alert_service.raise_alert()'s dedup logic: an existing
unresolved alert for the same device_id+category is updated in place
(bumping these two columns) instead of a new row being inserted on every
poll cycle, which is what made "Clear Alerts" look broken -- the next poll
would immediately recreate duplicates for any condition still active.
"""
import sqlalchemy as sa

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "alerts" not in inspector.get_table_names():
        return

    columns = {c["name"] for c in inspector.get_columns("alerts")}

    if "last_seen_at" not in columns:
        op.add_column("alerts", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    if "occurrence_count" not in columns:
        op.add_column(
            "alerts", sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1")
        )
        # Backfill last_seen_at for any pre-existing rows so it's never
        # NULL for alerts created before this migration.
        op.execute("UPDATE alerts SET last_seen_at = created_at WHERE last_seen_at IS NULL")


def downgrade() -> None:
    op.drop_column("alerts", "occurrence_count")
    op.drop_column("alerts", "last_seen_at")

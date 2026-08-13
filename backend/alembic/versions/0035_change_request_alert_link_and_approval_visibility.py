"""change_request_alert_link_and_approval_visibility

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-06 00:00:00.000000

Adds:
  - change_requests.triggering_alert_id: auto-link a CR to the alert/
    incident that triggered it (postmortem traceability, FK -> alerts.id).
  - change_requests.approved_at: timestamp of the final approval, used by
    the approval-workflow visibility / pending-approvals SLA views so we
    don't have to infer it from updated_at (which later pipeline stages
    also touch).
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import (
    add_column_if_missing,
    create_foreign_key_if_missing,
    create_index_if_missing,
    drop_column_if_exists,
    drop_index_if_exists,
)

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column in [c["name"] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if not _has_column("change_requests", "triggering_alert_id"):
        add_column_if_missing(
            "change_requests",
            sa.Column("triggering_alert_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        create_foreign_key_if_missing(
            "fk_change_requests_triggering_alert_id",
            "change_requests",
            "alerts",
            ["triggering_alert_id"],
            ["id"],
        )
        create_index_if_missing(
            "ix_change_requests_triggering_alert_id",
            "change_requests",
            ["triggering_alert_id"],
        )

    if not _has_column("change_requests", "approved_at"):
        add_column_if_missing(
            "change_requests",
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    if _has_column("change_requests", "approved_at"):
        drop_column_if_exists("change_requests", "approved_at")
    if _has_column("change_requests", "triggering_alert_id"):
        drop_index_if_exists("ix_change_requests_triggering_alert_id", table_name="change_requests")
        op.drop_constraint(
            "fk_change_requests_triggering_alert_id", "change_requests", type_="foreignkey"
        )
        drop_column_if_exists("change_requests", "triggering_alert_id")

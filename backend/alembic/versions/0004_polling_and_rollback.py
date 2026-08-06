"""add health-monitor polling columns + change_request rollback tracking

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-31

Context: Real-Time Health Monitoring (FR-9 / SRS 6.7) previously ran the
health suite exactly once immediately after a deploy instead of actually
polling it across a monitoring window. app.services.health_monitor now
has run_monitoring_window(), which produces one poll round per interval;
health_check_results.poll_round / elapsed_seconds record which round a
given check result belongs to so the monitoring history is reconstructable.

Also adds a proper manual/self-healing rollback trail: change_requests.
is_rollback + rollback_snapshot_id, so a rollback triggered from the UI
(app.services.rollback_service) is a normal, auditable ChangeRequest that
happens to redeploy a prior ConfigSnapshot, rather than an untracked
side-effect.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    hcr_columns = {c["name"] for c in inspector.get_columns("health_check_results")}
    if "poll_round" not in hcr_columns:
        op.add_column(
            "health_check_results",
            sa.Column("poll_round", sa.Integer(), nullable=False, server_default="1"),
        )
    if "elapsed_seconds" not in hcr_columns:
        op.add_column(
            "health_check_results",
            sa.Column("elapsed_seconds", sa.Integer(), nullable=False, server_default="0"),
        )

    cr_columns = {c["name"] for c in inspector.get_columns("change_requests")}
    if "is_rollback" not in cr_columns:
        op.add_column(
            "change_requests",
            sa.Column("is_rollback", sa.String(), nullable=False, server_default="false"),
        )
    if "rollback_snapshot_id" not in cr_columns:
        op.add_column(
            "change_requests",
            sa.Column(
                "rollback_snapshot_id", UUID(as_uuid=True), sa.ForeignKey("config_snapshots.id"), nullable=True
            ),
        )


def downgrade() -> None:
    op.drop_column("change_requests", "rollback_snapshot_id")
    op.drop_column("change_requests", "is_rollback")
    op.drop_column("health_check_results", "elapsed_seconds")
    op.drop_column("health_check_results", "poll_round")

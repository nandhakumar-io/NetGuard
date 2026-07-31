"""add config_drifts table (Config Drift Detection)

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-31
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

drift_severity = sa.Enum("none", "low", "medium", "high", name="driftseverity")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "config_drifts" in inspector.get_table_names():
        return  # already created by baseline's create_all(checkfirst=True) on a fresh install

    drift_severity.create(bind, checkfirst=True)

    op.create_table(
        "config_drifts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("device_id", UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=False),
        sa.Column(
            "baseline_snapshot_id", UUID(as_uuid=True), sa.ForeignKey("config_snapshots.id"), nullable=True
        ),
        sa.Column("drifted", sa.String(), nullable=False, server_default="false"),
        sa.Column("severity", drift_severity, nullable=False, server_default="none"),
        sa.Column("lines_changed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("diff", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("triggered_by", sa.String(), nullable=False, server_default="scheduled"),
        sa.Column("resolved", sa.String(), nullable=False, server_default="false"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_config_drifts_device_id", "config_drifts", ["device_id"])


def downgrade() -> None:
    op.drop_index("ix_config_drifts_device_id", table_name="config_drifts")
    op.drop_table("config_drifts")
    drift_severity.drop(op.get_bind(), checkfirst=True)

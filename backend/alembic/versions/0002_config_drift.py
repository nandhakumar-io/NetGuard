"""add config_drifts table (Config Drift Detection)

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-31

Schema matches app.models.config_drift.ConfigDrift exactly.  The model uses
Python-level Enum backed by VARCHAR (native_enum=False, which is SQLAlchemy's
default when the Python type is a str-mixin enum), so no PostgreSQL native
enum type is created here.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "config_drifts" in inspector.get_table_names():
        return  # already created by baseline's create_all(checkfirst=True) on a fresh install

    op.create_table(
        "config_drifts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("device_id", UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=False),
        # DriftBaseline: 'golden_config' | 'previous_backup'
        sa.Column("baseline", sa.String(length=15), nullable=False, server_default="previous_backup"),
        sa.Column("diff_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("added_lines", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("removed_lines", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("modified_lines", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("risk_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("compliance_score", sa.Integer(), nullable=False, server_default="100"),
        # DriftSeverity: 'low' | 'medium' | 'high' | 'critical'
        sa.Column("severity", sa.String(length=8), nullable=False, server_default="low"),
        sa.Column("ai_summary", sa.Text(), nullable=True),
        # DriftStatus: 'open' | 'approved' | 'rolled_back' | 'dismissed'
        sa.Column("status", sa.String(length=11), nullable=False, server_default="open"),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_config_drifts_device_id", "config_drifts", ["device_id"])
    op.create_index("ix_config_drifts_detected_at", "config_drifts", ["detected_at"])


def downgrade() -> None:
    op.drop_index("ix_config_drifts_detected_at", table_name="config_drifts")
    op.drop_index("ix_config_drifts_device_id", table_name="config_drifts")
    op.drop_table("config_drifts")

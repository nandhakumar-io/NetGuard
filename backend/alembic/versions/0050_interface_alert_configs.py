"""Add interface_alert_configs table

Revision ID: 0050
Revises: 0049
Create Date: 2026-08-13

Backs the per-interface "alert me if this port goes down" toggle on the
device Interfaces tab -- see app.models.interface_alert_config for why
this exists (mutes noisy per-port critical alerts without disabling the
Interface Down feature globally).
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "interface_alert_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=False, index=True),
        sa.Column("if_descr", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("device_id", "if_descr", name="uq_interface_alert_config_device_if"),
    )


def downgrade() -> None:
    op.drop_table("interface_alert_configs")

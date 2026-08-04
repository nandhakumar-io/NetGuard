"""devices.device_role column + compliance_baselines table

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-03

Adds the role-based compliance baseline feature: Device.device_role (a
free-text role like "core"/"distribution"/"access"/"edge-firewall",
distinct from device_type) and the compliance_baselines table backing
app.models.compliance_baseline.ComplianceBaseline -- one shared baseline
template per role instead of forcing every device onto either its own
individual GoldenConfig or none at all. See DriftBaseline.ROLE_BASELINE in
app.services.drift_service._resolve_baseline_config.

Uses add_column_if_missing / create_table_if_missing / create_index_if_missing,
same reasoning as 0018/0021 -- safe on a fresh install (0001's create_all
already sees these since they're part of the models by then) and on an
existing database that's already past 0001.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

from migration_helpers import add_column_if_missing, create_index_if_missing, create_table_if_missing

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("devices", sa.Column("device_role", sa.String(), nullable=True))

    create_table_if_missing(
        "compliance_baselines",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("device_role", sa.String(), nullable=False, unique=True),
        sa.Column("config_encrypted", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(), nullable=False),
        sa.Column("set_by", sa.String(), nullable=False, server_default="system"),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    create_index_if_missing(
        "ix_compliance_baselines_device_role", "compliance_baselines", ["device_role"], unique=True
    )


def downgrade() -> None:
    op.drop_table("compliance_baselines")
    op.drop_column("devices", "device_role")
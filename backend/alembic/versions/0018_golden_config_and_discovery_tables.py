"""golden_configs + discovered_neighbors tables

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-03

Context: app.models.golden_config.GoldenConfig has existed for a while
(drift_service already reads GoldenConfig rows when a device's
ConfigDrift.baseline is GOLDEN_CONFIG, and app.api.config_management
already exposes GET/PUT/DELETE .../golden-config) but the model was never
imported into app/models/__init__.py and no migration ever created its
table. On both a fresh install (0001's create_all only sees imported
models) and an existing database, every golden-config read/write 500s
with "relation golden_configs does not exist". This migration creates it,
plus the new discovered_neighbors table backing persisted LLDP/CDP
discovery results (see app.models.discovered_neighbor).

Uses create_table_if_missing / create_index_if_missing so this is also
safe to run against a database where some other process already created
either table by hand while debugging.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op
from migration_helpers import create_index_if_missing, create_table_if_missing

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_table_if_missing(
        "golden_configs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("device_id", UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=False, unique=True),
        sa.Column("config_encrypted", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(), nullable=False),
        sa.Column("set_by", sa.String(), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    create_index_if_missing("ix_golden_configs_device_id", "golden_configs", ["device_id"])

    create_table_if_missing(
        "discovered_neighbors",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("device_id", UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=False),
        sa.Column("protocol", sa.String(length=8), nullable=False),
        sa.Column("local_port", sa.String(length=64), nullable=True),
        sa.Column("neighbor_name", sa.String(length=255), nullable=True),
        sa.Column("neighbor_port", sa.String(length=255), nullable=True),
        sa.Column("neighbor_platform", sa.String(length=255), nullable=True),
        sa.Column("neighbor_device_id", UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    create_index_if_missing("ix_discovered_neighbors_device_id", "discovered_neighbors", ["device_id"])
    create_index_if_missing("ix_discovered_neighbors_neighbor_device_id", "discovered_neighbors", ["neighbor_device_id"])


def downgrade() -> None:
    op.drop_table("discovered_neighbors")
    op.drop_table("golden_configs")

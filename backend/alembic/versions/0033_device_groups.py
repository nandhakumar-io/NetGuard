"""add device_groups table + devices.group_id

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-05

Backs app.api.device_groups, which already shipped with full CRUD +
device-assignment endpoints but was never wired up because its model
(app.models.device_group.DeviceGroup) and the devices.group_id column it
assigns into didn't exist yet. This is the named/logical group a user
creates explicitly ("Edge Firewalls", "Q3 Migration Batch"), with optional
nesting via parent_group_id -- distinct from the free-text
data_center/rack columns added in 0032, which model physical placement.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import (
    add_column_if_missing,
    create_index_if_missing,
    create_table_if_missing,
)

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade():
    create_table_if_missing(
        "device_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("group_type", sa.String(), nullable=False, server_default="static"),
        sa.Column("parent_group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("device_groups.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    create_index_if_missing("ix_device_groups_name", "device_groups", ["name"])
    create_index_if_missing("ix_device_groups_parent_group_id", "device_groups", ["parent_group_id"])

    add_column_if_missing("devices", sa.Column("group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("device_groups.id"), nullable=True))
    create_index_if_missing("ix_devices_group_id", "devices", ["group_id"])


def downgrade():
    op.drop_index("ix_devices_group_id", table_name="devices")
    op.drop_column("devices", "group_id")

    op.drop_index("ix_device_groups_parent_group_id", table_name="device_groups")
    op.drop_index("ix_device_groups_name", table_name="device_groups")
    op.drop_table("device_groups")

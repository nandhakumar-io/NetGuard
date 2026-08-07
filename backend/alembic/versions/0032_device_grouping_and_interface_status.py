"""add device grouping (data center + rack) and interface status history

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-05

Two additions for NOC-style operations:

1. Device grouping. `devices.data_center` / `devices.rack` /
   `devices.rack_position` let devices be grouped hierarchically
   (data center -> rack -> device) in the Topology view and Device
   Inventory, the same free-text convention as the existing `site` /
   `device_type` / `device_role` columns.

2. Interface status history. `interface_statuses` is a transition log
   (one row per detected ifOperStatus change per device/ifIndex), used
   to raise "Interface Down" alerts, drive the NOC dashboard's live
   down-port list, and back a per-device port history view. See
   app.models.interface_status and app.services.metrics_service.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import (
    add_column_if_missing,
    create_index_if_missing,
    create_table_if_missing,
)

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing("devices", sa.Column("data_center", sa.String(), nullable=True))
    add_column_if_missing("devices", sa.Column("rack", sa.String(), nullable=True))
    add_column_if_missing("devices", sa.Column("rack_position", sa.Integer(), nullable=True))
    create_index_if_missing("ix_devices_data_center", "devices", ["data_center"])
    create_index_if_missing("ix_devices_rack", "devices", ["rack"])

    create_table_if_missing(
        "interface_statuses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=False),
        sa.Column("if_index", sa.String(), nullable=False),
        sa.Column("if_descr", sa.String(), nullable=False),
        sa.Column("status", sa.Enum("up", "down", name="interfaceoperstatus"), nullable=False),
        sa.Column("previous_status", sa.Enum("up", "down", name="interfaceoperstatus"), nullable=True),
        sa.Column("is_transition", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    create_index_if_missing("ix_interface_statuses_device_id", "interface_statuses", ["device_id"])
    create_index_if_missing("ix_interface_statuses_changed_at", "interface_statuses", ["changed_at"])
    create_index_if_missing(
        "ix_interface_statuses_device_ifindex", "interface_statuses", ["device_id", "if_index", "changed_at"]
    )


def downgrade():
    op.drop_index("ix_interface_statuses_device_ifindex", table_name="interface_statuses")
    op.drop_index("ix_interface_statuses_changed_at", table_name="interface_statuses")
    op.drop_index("ix_interface_statuses_device_id", table_name="interface_statuses")
    op.drop_table("interface_statuses")
    sa.Enum(name="interfaceoperstatus").drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_devices_rack", table_name="devices")
    op.drop_index("ix_devices_data_center", table_name="devices")
    op.drop_column("devices", "rack_position")
    op.drop_column("devices", "rack")
    op.drop_column("devices", "data_center")

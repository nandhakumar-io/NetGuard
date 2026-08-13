"""devices.netbox_id / netbox_last_synced_at

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-04

Backs app.services.netbox_service's pull-sync (NetBox -> NetGuard device
inventory). netbox_id is the match key used to update an existing device
on re-sync instead of creating a duplicate when it's renamed in NetBox;
nullable/unique so manually-added and GNS3-discovered devices (which have
no NetBox counterpart) are unaffected.
"""
import sqlalchemy as sa

from migration_helpers import (
    add_column_if_missing,
    create_index_if_missing,
    drop_column_if_exists,
    drop_index_if_exists,
)

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("devices", sa.Column("netbox_id", sa.Integer(), nullable=True))
    add_column_if_missing("devices", sa.Column("netbox_last_synced_at", sa.DateTime(timezone=True), nullable=True))
    create_index_if_missing("ix_devices_netbox_id", "devices", ["netbox_id"], unique=True)


def downgrade() -> None:
    drop_index_if_exists("ix_devices_netbox_id", table_name="devices")
    drop_column_if_exists("devices", "netbox_last_synced_at")
    drop_column_if_exists("devices", "netbox_id")

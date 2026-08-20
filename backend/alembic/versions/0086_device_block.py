"""device block (topology grouping)

Revision ID: 0086
Revises: 0085
Create Date: 2026-08-20

Adds devices.block: an optional grouping level between data_center and
rack, so the Groups page can model a real campus/DC hierarchy (building
or pod "blocks" containing multiple racks) instead of a flat
DC -> rack -> device tree. See app.models.device.Device.block and
GET /devices/groups/summary.
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("devices", sa.Column("block", sa.String(), nullable=True))
    op_create_index()


def op_create_index() -> None:
    from alembic import op

    try:
        op.create_index("ix_devices_block", "devices", ["block"])
    except Exception:
        pass


def downgrade() -> None:
    from alembic import op

    try:
        op.drop_index("ix_devices_block", table_name="devices")
    except Exception:
        pass
    op.drop_column("devices", "block")

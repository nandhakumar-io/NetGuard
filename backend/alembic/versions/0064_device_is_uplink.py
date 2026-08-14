"""Devices: explicit is_uplink flag for WAN/uplink monitoring

Revision ID: 0064
Revises: 0063
Create Date: 2026-08-14 00:00:00.000000

Adds a real boolean an operator can set from the Devices page, instead
of the Uplinks & WAN Links dashboard widget relying purely on
device_role containing a magic keyword (wan/uplink/edge/core/isp/
internet). See app.models.device.Device.is_uplink's docstring.
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "devices",
        sa.Column("is_uplink", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    from alembic import op

    op.drop_column("devices", "is_uplink")

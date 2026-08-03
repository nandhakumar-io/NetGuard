"""devices.netconf_use_lock

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-03

Adds a per-device toggle for whether netconf_service.push_config should
<lock>/<unlock> the target datastore around edit-config (see the column
docstring on app.models.device.Device.netconf_use_lock). Defaults to true
(the previous, always-lock behavior) for every existing row, so this is a
pure opt-out -- no device's behavior changes until an operator flips it.
"""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("netconf_use_lock", sa.Boolean(), nullable=False, server_default="true"),
    )


def downgrade() -> None:
    op.drop_column("devices", "netconf_use_lock")
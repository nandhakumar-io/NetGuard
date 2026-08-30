"""Devices: explicit is_pinned_critical flag for the Core & Critical shortlist

Revision ID: 0117
Revises: 0116
Create Date: 2026-08-30

The Core & Critical Devices shortlist on the Devices page used to be
entirely heuristic (is_uplink OR device_role containing a core-ish
keyword OR currently unhealthy) -- on a small fleet where most devices
happen to be flagged uplink or briefly degraded, that heuristic-only
shortlist ended up just being the whole inventory, with no way for an
operator to actually curate it. This adds a real boolean an operator can
set from the Devices page. See app.models.device.Device.is_pinned_critical's
docstring.
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing

revision = "0117"
down_revision = "0116"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "devices",
        sa.Column("is_pinned_critical", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    from alembic import op

    op.drop_column("devices", "is_pinned_critical")

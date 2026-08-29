"""physical_locations table (empty block/data-center/rack placeholders)

Revision ID: 0106
Revises: 0105
Create Date: 2026-08-29

The Groups page's Data Center/Rack view previously derived every
block/data_center/rack entirely from Device.block/data_center/rack --
so a tier with zero devices in it had no row anywhere to exist as, and
"create a data center to plan a build-out in before any device lands
in it" or "rename/delete an emptied-out rack" were both impossible
(rename/delete had to move at least one device to have anything to
act on). This table gives each named tier a row independent of device
membership; see app.models.physical_location.PhysicalLocation and
app.api.devices.get_device_groups, which now merges these rows into
the same tree it builds from live devices.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op
from migration_helpers import create_table_if_missing, index_exists

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_table_if_missing(
        "physical_locations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("block", sa.String(), nullable=False),
        sa.Column("data_center", sa.String(), nullable=True),
        sa.Column("rack", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("block", "data_center", "rack", name="uq_physical_location_tier"),
    )
    if not index_exists("physical_locations", "ix_physical_locations_block"):
        op.create_index("ix_physical_locations_block", "physical_locations", ["block"])
    if not index_exists("physical_locations", "ix_physical_locations_data_center"):
        op.create_index("ix_physical_locations_data_center", "physical_locations", ["data_center"])
    if not index_exists("physical_locations", "ix_physical_locations_rack"):
        op.create_index("ix_physical_locations_rack", "physical_locations", ["rack"])


def downgrade() -> None:
    op.drop_table("physical_locations")

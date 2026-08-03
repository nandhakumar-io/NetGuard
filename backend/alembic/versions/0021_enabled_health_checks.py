"""devices.enabled_health_checks column

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-03

app.models.device.Device.enabled_health_checks (JSON-encoded list of
check names, NULL = run everything) backs the new "select which health
checks to run" picker on the device edit form and is read by
pipeline_service._verify (see the enabled_checks filtering added there).
The column was added to the model but never picked up by a migration --
same class of bug as golden_configs in 0018: fine on a brand-new install
(0001's create_all sees the column since it's part of the model by then),
silently missing on any database that was already migrated past 0001
before this column was added.
"""
import sqlalchemy as sa
from alembic import op

from migration_helpers import add_column_if_missing

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("devices", sa.Column("enabled_health_checks", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("devices", "enabled_health_checks")
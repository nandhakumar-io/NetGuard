"""device lifecycle state, tags, custom fields, dynamic group rules

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-09

Adds inventory-management columns requested for the Devices/Inventory and
Groups pages:

  - devices.lifecycle_state: staging -> production -> decommissioned,
    independent of the existing online/offline/degraded `status`.
  - devices.tags: JSON-encoded list of free-text labels, usable for
    ad-hoc filtering, bulk "assign tags", and dynamic group rules.
  - devices.custom_fields: JSON-encoded string->string map for
    org-defined fields (asset tag, owner team, cost center, ...)
    without a migration per field.
  - device_groups.is_dynamic / membership_rules: lets a group auto-add
    members by matching hostname/tag/site/device_type/device_role glob
    patterns instead of only explicit assignment.

See app.models.device, app.models.device_group, and
app.services.group_membership_service.
"""
import sqlalchemy as sa

from alembic import op
from migration_helpers import add_column_if_missing, enum_type_exists

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql" and not enum_type_exists("devicelifecyclestate"):
        lifecycle_enum = sa.Enum("staging", "production", "decommissioned", name="devicelifecyclestate")
        lifecycle_enum.create(op.get_bind(), checkfirst=True)

    add_column_if_missing(
        "devices",
        sa.Column(
            "lifecycle_state",
            sa.Enum("staging", "production", "decommissioned", name="devicelifecyclestate"),
            nullable=False,
            server_default="production",
        ),
    )
    add_column_if_missing("devices", sa.Column("tags", sa.Text(), nullable=True))
    add_column_if_missing("devices", sa.Column("custom_fields", sa.Text(), nullable=True))

    add_column_if_missing(
        "device_groups",
        sa.Column("is_dynamic", sa.Boolean(), nullable=False, server_default="false"),
    )
    add_column_if_missing("device_groups", sa.Column("membership_rules", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("device_groups", "membership_rules")
    op.drop_column("device_groups", "is_dynamic")
    op.drop_column("devices", "custom_fields")
    op.drop_column("devices", "tags")
    op.drop_column("devices", "lifecycle_state")
    op.execute("DROP TYPE IF EXISTS devicelifecyclestate")

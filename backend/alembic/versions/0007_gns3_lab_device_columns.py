"""GNS3 lab device columns on devices table

Revision ID: 0007
Revises: 85cec3c5accf
Create Date: 2026-07-31

Adds the lab/simulation columns that let Device inventory rows point at a
backing GNS3 node (project/node ids, console host/port, bootstrapped flag).
These were already present on the SQLAlchemy model and Pydantic schemas;
this migration brings the database in line so create/sync/bootstrap paths
don't fail on missing columns.
"""
import sqlalchemy as sa

from alembic import op
from migration_helpers import add_column_if_missing, create_index_if_missing

revision = "0007"
down_revision = "85cec3c5accf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Guarded: on a fresh install these columns already exist courtesy of
    # 0001's create_all(checkfirst=True) against today's Device model.
    add_column_if_missing(
        "devices",
        sa.Column("is_simulated", sa.Boolean(), server_default="false", nullable=False),
    )
    add_column_if_missing("devices", sa.Column("lab_provider", sa.String(), nullable=True))
    add_column_if_missing("devices", sa.Column("gns3_project_id", sa.String(), nullable=True))
    add_column_if_missing("devices", sa.Column("gns3_node_id", sa.String(), nullable=True))
    add_column_if_missing("devices", sa.Column("console_host", sa.String(), nullable=True))
    add_column_if_missing("devices", sa.Column("console_port", sa.Integer(), nullable=True))
    add_column_if_missing(
        "devices",
        sa.Column("console_type", sa.String(), server_default="telnet", nullable=True),
    )
    add_column_if_missing(
        "devices",
        sa.Column("bootstrapped", sa.Boolean(), server_default="false", nullable=False),
    )
    create_index_if_missing(
        "ix_devices_gns3_project_node",
        "devices",
        ["gns3_project_id", "gns3_node_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_devices_gns3_project_node", table_name="devices")
    op.drop_column("devices", "bootstrapped")
    op.drop_column("devices", "console_type")
    op.drop_column("devices", "console_port")
    op.drop_column("devices", "console_host")
    op.drop_column("devices", "gns3_node_id")
    op.drop_column("devices", "gns3_project_id")
    op.drop_column("devices", "lab_provider")
    op.drop_column("devices", "is_simulated")

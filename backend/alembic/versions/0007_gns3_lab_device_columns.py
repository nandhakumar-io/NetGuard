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

revision = "0007"
down_revision = "85cec3c5accf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("is_simulated", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column("devices", sa.Column("lab_provider", sa.String(), nullable=True))
    op.add_column("devices", sa.Column("gns3_project_id", sa.String(), nullable=True))
    op.add_column("devices", sa.Column("gns3_node_id", sa.String(), nullable=True))
    op.add_column("devices", sa.Column("console_host", sa.String(), nullable=True))
    op.add_column("devices", sa.Column("console_port", sa.Integer(), nullable=True))
    op.add_column(
        "devices",
        sa.Column("console_type", sa.String(), server_default="telnet", nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("bootstrapped", sa.Boolean(), server_default="false", nullable=False),
    )
    op.create_index(
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
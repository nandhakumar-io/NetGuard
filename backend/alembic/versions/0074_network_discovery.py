"""Network Discovery: discovery_scans, discovered_hosts tables

Revision ID: 0074
Revises: 0073
Create Date: 2026-08-19 00:00:00.000000

Adds subnet-wide "sweep a CIDR range for live hosts" scans, distinct
from the existing per-device SNMP/LLDP/CDP discovery (DiscoveredNeighbor)
which only ever looks at neighbors of a device NetGuard already knows
about. See app.models.network_discovery and
app.services.network_discovery_service.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import (
    add_column_if_missing,
    create_index_if_missing,
    create_table_if_missing,
)

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_table_if_missing(
        "discovery_scans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("cidr", sa.String(), nullable=False),
        sa.Column("snmp_community_ref", sa.String(), nullable=True),
        sa.Column("ports", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("total_hosts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("responsive_hosts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new_hosts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_by", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    create_table_if_missing(
        "discovered_hosts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scan_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("discovery_scans.id"), nullable=False,
        ),
        sa.Column("ip_address", sa.String(), nullable=False),
        sa.Column("ip_sort_key", sa.String(), nullable=False),
        sa.Column("hostname", sa.String(), nullable=True),
        sa.Column("mac_address", sa.String(), nullable=True),
        sa.Column("open_ports", sa.String(), nullable=True),
        sa.Column("snmp_sys_name", sa.String(), nullable=True),
        sa.Column("snmp_sys_descr", sa.Text(), nullable=True),
        sa.Column("vendor_guess", sa.String(), nullable=True),
        sa.Column("response_time_ms", sa.Float(), nullable=True),
        sa.Column("matched_device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=True),
        sa.Column("imported", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("imported_device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=True),
        sa.Column("ignored", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("discovered_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    create_index_if_missing("ix_discovered_hosts_scan_id", "discovered_hosts", ["scan_id"])
    create_index_if_missing("ix_discovered_hosts_ip_sort_key", "discovered_hosts", ["ip_sort_key"])

    # discovery_schedules references discovery_scans.id (last_scan_id), and
    # discovery_scans references discovery_schedules.id (schedule_id) --
    # a genuine circular FK between the two tables. Broken by creating
    # discovery_scans first (above, without schedule_id), then
    # discovery_schedules here, then adding discovery_scans.schedule_id
    # as a follow-up column once both tables exist.
    create_table_if_missing(
        "discovery_schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("cidr", sa.String(), nullable=False),
        sa.Column("snmp_community_ref", sa.String(), nullable=True),
        sa.Column("ports", sa.String(), nullable=True),
        sa.Column("interval_minutes", sa.Integer(), nullable=False, server_default="1440"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("discovery_scans.id"), nullable=True),
    )

    add_column_if_missing(
        "discovery_scans",
        sa.Column("schedule_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("discovery_schedules.id"), nullable=True),
    )
    create_index_if_missing("ix_discovery_scans_schedule_id", "discovery_scans", ["schedule_id"])


def downgrade() -> None:
    op.drop_table("discovered_hosts")
    op.drop_column("discovery_scans", "schedule_id")
    op.drop_table("discovery_schedules")
    op.drop_table("discovery_scans")

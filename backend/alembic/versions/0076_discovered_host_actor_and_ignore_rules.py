"""Discovered host actor trail + persistent per-schedule ignore rules

Revision ID: 0076
Revises: 0075
Create Date: 2026-08-19 00:00:00.000000

Two additions to network discovery, both incident-response driven:

1. discovered_hosts gains imported_by/imported_at/ignored_by/ignored_at
   so "who imported this rogue device without review" has an answer --
   previously the only actor on record was DiscoveryScan.started_by,
   which identifies who kicked off the *scan*, not who actioned any
   individual host in its results.

2. discovery_ignore_rules persists an ignore decision per
   (schedule_id, ip_address, vendor_guess) fingerprint, so a recurring
   DiscoverySchedule stops re-flagging a host someone already reviewed
   and dismissed. See app.models.network_discovery.DiscoveryIgnoreRule.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import add_column_if_missing

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("discovered_hosts", sa.Column("imported_by", sa.String(), nullable=True))
    add_column_if_missing("discovered_hosts", sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True))
    add_column_if_missing("discovered_hosts", sa.Column("ignored_by", sa.String(), nullable=True))
    add_column_if_missing("discovered_hosts", sa.Column("ignored_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "discovery_ignore_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "schedule_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("discovery_schedules.id"),
            nullable=False,
        ),
        sa.Column("ip_address", sa.String(), nullable=False),
        sa.Column("vendor_guess", sa.String(), nullable=True),
        sa.Column("ignored_by", sa.String(), nullable=False),
        sa.Column("ignored_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("note", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_discovery_ignore_rules_schedule_id", "discovery_ignore_rules", ["schedule_id"]
    )
    # Partial-ish dedup guard: NULL vendor_guess rows aren't caught by a
    # plain unique constraint (NULLs never compare equal), so the app
    # layer (ignore_host) does a query-then-upsert instead of relying on
    # a DB constraint to prevent duplicate rules for the same fingerprint.
    op.create_index(
        "ix_discovery_ignore_rules_fingerprint",
        "discovery_ignore_rules",
        ["schedule_id", "ip_address", "vendor_guess"],
    )


def downgrade() -> None:
    op.drop_index("ix_discovery_ignore_rules_fingerprint", table_name="discovery_ignore_rules")
    op.drop_index("ix_discovery_ignore_rules_schedule_id", table_name="discovery_ignore_rules")
    op.drop_table("discovery_ignore_rules")
    op.drop_column("discovered_hosts", "ignored_at")
    op.drop_column("discovered_hosts", "ignored_by")
    op.drop_column("discovered_hosts", "imported_at")
    op.drop_column("discovered_hosts", "imported_by")

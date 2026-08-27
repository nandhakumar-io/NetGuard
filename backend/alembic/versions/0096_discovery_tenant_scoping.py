"""Tenant scoping for network discovery

Revision ID: 0096
Revises: 0095

Continues the tenancy audit started in 0095_approval_and_tenant_scoping:
discovery_scans and discovery_schedules were flagged there as unscoped
("discovery scans" in that migration's docstring) -- any authenticated
user, regardless of tenant, could see and act on every other tenant's
network discovery scans/results and recurring schedules. This closes
that gap the same way 0095 did for webhook_endpoints/alert_rules:
nullable + backfilled onto the "Default" tenant (NULL == MSP-staff-
initiated / global-visibility, same convention throughout).

DiscoveredHost and DiscoveryIgnoreRule intentionally do NOT get their
own tenant_id -- they're always reached through their parent
DiscoveryScan/DiscoverySchedule (scan_id / schedule_id FK), so scoping
rides on the parent the same way Deployment rides on Device.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import (
    add_column_if_missing,
    create_foreign_key_if_missing,
    create_index_if_missing,
)

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "discovery_scans", sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    create_index_if_missing("ix_discovery_scans_tenant_id", "discovery_scans", ["tenant_id"])
    create_foreign_key_if_missing(
        "fk_discovery_scans_tenant_id", "discovery_scans", "tenants", ["tenant_id"], ["id"],
    )

    add_column_if_missing(
        "discovery_schedules", sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    create_index_if_missing("ix_discovery_schedules_tenant_id", "discovery_schedules", ["tenant_id"])
    create_foreign_key_if_missing(
        "fk_discovery_schedules_tenant_id", "discovery_schedules", "tenants", ["tenant_id"], ["id"],
    )

    # Backfill both onto "Default", same rationale as 0095/0092: existing
    # rows predate this scoping and belong to whichever tenant was
    # "everyone" before tenants existed.
    conn = op.get_bind()
    default_tenant_id = conn.execute(
        sa.text("SELECT id FROM tenants WHERE slug = 'default' LIMIT 1")
    ).scalar()
    if default_tenant_id is not None:
        conn.execute(
            sa.text("UPDATE discovery_scans SET tenant_id = :tid WHERE tenant_id IS NULL"),
            {"tid": default_tenant_id},
        )
        conn.execute(
            sa.text("UPDATE discovery_schedules SET tenant_id = :tid WHERE tenant_id IS NULL"),
            {"tid": default_tenant_id},
        )


def downgrade() -> None:
    op.drop_constraint("fk_discovery_schedules_tenant_id", "discovery_schedules", type_="foreignkey")
    op.drop_index("ix_discovery_schedules_tenant_id", table_name="discovery_schedules")
    op.drop_column("discovery_schedules", "tenant_id")

    op.drop_constraint("fk_discovery_scans_tenant_id", "discovery_scans", type_="foreignkey")
    op.drop_index("ix_discovery_scans_tenant_id", table_name="discovery_scans")
    op.drop_column("discovery_scans", "tenant_id")

"""Registration approval gate + tenant scoping for webhooks/alert rules

Revision ID: 0095
Revises: 0094

Two unrelated additions bundled into one migration because both are
single-column, backfill-and-default additive changes with no data
transform:

1. users.is_approved (Boolean, NOT NULL, server_default true) -- backs
   the admin-approval registration workflow (app.api.auth.register /
   app.api.user_management.approve_user). server_default=true grandfathers
   in every existing account (including anyone mid-session right now) so
   this migration can never lock out an existing user; POST /auth/register
   is the only code path that explicitly writes False for a brand-new row.

2. webhook_endpoints.tenant_id and alert_rules.tenant_id (nullable UUID
   FK -> tenants.id) -- these two tables were flagged in the tenancy audit
   as customer-configurable resources (outbound notification targets,
   alerting thresholds) that were completely unscoped: any authenticated
   user, regardless of tenant, could list/edit/delete every other
   tenant's webhooks and alert rules. Nullable + backfilled onto the
   "Default" tenant (same convention as migration 0092_tenants) rather
   than NOT NULL, since an MSP-staff-authored global rule/webhook is a
   legitimate tenant_id=NULL state (see app.core.deps.get_tenant_scope).
   This is a partial pass, not the full retrofit -- see /areas/netguard6
   tenancy audit for the remaining unscoped routers (discovery scans,
   backups*, deployments/change-requests, notification settings, IPAM,
   GitOps, ...). (*backup_jobs is intentionally excluded: it's a whole-
   install pg_dump, not a per-tenant resource.)
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_approved", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    op.add_column(
        "webhook_endpoints",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_webhook_endpoints_tenant_id", "webhook_endpoints", ["tenant_id"])
    op.create_foreign_key(
        "fk_webhook_endpoints_tenant_id", "webhook_endpoints", "tenants", ["tenant_id"], ["id"],
    )

    op.add_column(
        "alert_rules",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_alert_rules_tenant_id", "alert_rules", ["tenant_id"])
    op.create_foreign_key(
        "fk_alert_rules_tenant_id", "alert_rules", "tenants", ["tenant_id"], ["id"],
    )

    # Backfill both new tenant_id columns onto the "Default" tenant, same
    # rationale as 0092_tenants: existing rows predate multi-tenancy and
    # belong to whichever tenant was "everyone" before tenants existed.
    # If no Default tenant exists yet (fresh install, migration run before
    # any tenant is created), this is a no-op and the columns stay NULL
    # (== global/MSP-visible), which is a safe default, not a broken one.
    conn = op.get_bind()
    default_tenant_id = conn.execute(
        sa.text("SELECT id FROM tenants WHERE slug = 'default' LIMIT 1")
    ).scalar()
    if default_tenant_id is not None:
        conn.execute(
            sa.text("UPDATE webhook_endpoints SET tenant_id = :tid WHERE tenant_id IS NULL"),
            {"tid": default_tenant_id},
        )
        conn.execute(
            sa.text("UPDATE alert_rules SET tenant_id = :tid WHERE tenant_id IS NULL"),
            {"tid": default_tenant_id},
        )


def downgrade() -> None:
    op.drop_constraint("fk_alert_rules_tenant_id", "alert_rules", type_="foreignkey")
    op.drop_index("ix_alert_rules_tenant_id", table_name="alert_rules")
    op.drop_column("alert_rules", "tenant_id")

    op.drop_constraint("fk_webhook_endpoints_tenant_id", "webhook_endpoints", type_="foreignkey")
    op.drop_index("ix_webhook_endpoints_tenant_id", table_name="webhook_endpoints")
    op.drop_column("webhook_endpoints", "tenant_id")

    op.drop_column("users", "is_approved")

"""Audit log tenant scoping + alert rule inheritance

Revision ID: 0097
Revises: 0096

Two changes, bundled because both were found while closing out the
tenancy audit started in 0095/0096:

1. audit_logs had NO tenant_id at all -- GET /audit-logs (app.api.audit)
   was filtering by nothing, so any authenticated user of any tenant
   could read every other tenant's audit history. Same "nullable,
   backfilled onto Default, NULL == global/system event" convention as
   0095/0096. Unlike discovery_scans/discovery_schedules, audit_logs
   rows are not all naturally tenant-owned (login events, MSP-staff
   actions) -- see app.services.audit_service.record_event, which now
   accepts an explicit tenant_id instead of this migration trying to
   infer one from device_hostname.

2. alert_rules gets parent_rule_id, a self-referential FK so a
   tenant-authored rule can explicitly declare which global (tenant_id
   IS NULL) rule it overrides -- either to re-threshold it or to
   suppress it (enabled=False + parent_rule_id set). Previously
   AlertRule.tenant_id only supported "global vs. tenant-owned" with no
   way to express one overriding the other; app.models.alert_rule_engine
   also had no tenant filter on its rule query at all, so a tenant's
   custom rule was being evaluated against every other tenant's devices
   too -- see the accompanying evaluate_rules() fix in
   app.models.alert_rule_engine.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import (
    add_column_if_missing,
    create_foreign_key_if_missing,
    create_index_if_missing,
)

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- audit_logs.tenant_id --
    add_column_if_missing(
        "audit_logs", sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    create_index_if_missing("ix_audit_logs_tenant_id", "audit_logs", ["tenant_id"])
    # Composite index: the MSP board's and the per-tenant audit page's hot
    # query is "this tenant's rows, newest first" -- see app.api.audit.
    create_index_if_missing(
        "ix_audit_logs_tenant_created", "audit_logs", ["tenant_id", "created_at"]
    )
    create_foreign_key_if_missing(
        "fk_audit_logs_tenant_id", "audit_logs", "tenants", ["tenant_id"], ["id"],
    )

    # Backfill: best-effort via device_hostname -> devices.tenant_id where
    # the device still exists and is unambiguous. Anything left NULL is
    # legitimately global/unattributable (system actions, login events,
    # rows whose device has since been deleted) -- same as leaving new
    # MSP-staff-authored rows NULL going forward, not an error condition.
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE audit_logs
            SET tenant_id = d.tenant_id
            FROM devices d
            WHERE audit_logs.device_hostname = d.hostname
              AND audit_logs.tenant_id IS NULL
              AND d.tenant_id IS NOT NULL
            """
        )
    )

    # -- alert_rules.parent_rule_id --
    add_column_if_missing(
        "alert_rules", sa.Column("parent_rule_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    create_index_if_missing("ix_alert_rules_parent_rule_id", "alert_rules", ["parent_rule_id"])
    create_foreign_key_if_missing(
        "fk_alert_rules_parent_rule_id", "alert_rules", "alert_rules",
        ["parent_rule_id"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_alert_rules_parent_rule_id", "alert_rules", type_="foreignkey")
    op.drop_index("ix_alert_rules_parent_rule_id", table_name="alert_rules")
    op.drop_column("alert_rules", "parent_rule_id")

    op.drop_constraint("fk_audit_logs_tenant_id", "audit_logs", type_="foreignkey")
    op.drop_index("ix_audit_logs_tenant_created", table_name="audit_logs")
    op.drop_index("ix_audit_logs_tenant_id", table_name="audit_logs")
    op.drop_column("audit_logs", "tenant_id")

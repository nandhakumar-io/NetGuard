"""Escalation policy tenant scoping + inheritance

Revision ID: 0098
Revises: 0097

escalation_policies had no tenant_id at all: app.api.escalation_policies
listed/created/updated/deleted every policy with zero tenant filtering,
and app.services.escalation_service.run_escalation_sweep evaluated every
enabled policy against every active alert fleet-wide -- so any tenant's
policy could page a contact over another tenant's unacknowledged alert.
Same gap AlertRule/WebhookEndpoint had before 0095, closed the same way:
nullable tenant_id, NULL == global/MSP-authored, applies to every tenant.

parent_policy_id mirrors AlertRule.parent_rule_id (0097): lets a tenant
explicitly override an MSP-default policy (different unack_minutes/
contacts) or suppress it (enabled=False + parent_policy_id set) instead
of only being able to add an unrelated policy alongside it. Answers "is
there an MSP-default on-call chain tenants inherit unless they set their
own" -- yes, via a global (tenant_id NULL) policy that a tenant policy
can link back to.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import (
    add_column_if_missing,
    create_foreign_key_if_missing,
    create_index_if_missing,
)

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "escalation_policies", sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    create_index_if_missing("ix_escalation_policies_tenant_id", "escalation_policies", ["tenant_id"])
    create_foreign_key_if_missing(
        "fk_escalation_policies_tenant_id", "escalation_policies", "tenants", ["tenant_id"], ["id"],
    )

    add_column_if_missing(
        "escalation_policies", sa.Column("parent_policy_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    create_index_if_missing(
        "ix_escalation_policies_parent_policy_id", "escalation_policies", ["parent_policy_id"]
    )
    create_foreign_key_if_missing(
        "fk_escalation_policies_parent_policy_id", "escalation_policies", "escalation_policies",
        ["parent_policy_id"], ["id"],
    )

    # Backfill existing rows onto "Default", same convention as every
    # other tenant-scoping migration -- pre-existing policies predate
    # tenants and belong to whichever tenant was "everyone" before.
    conn = op.get_bind()
    default_tenant_id = conn.execute(
        sa.text("SELECT id FROM tenants WHERE slug = 'default' LIMIT 1")
    ).scalar()
    if default_tenant_id is not None:
        conn.execute(
            sa.text("UPDATE escalation_policies SET tenant_id = :tid WHERE tenant_id IS NULL"),
            {"tid": default_tenant_id},
        )


def downgrade() -> None:
    op.drop_constraint(
        "fk_escalation_policies_parent_policy_id", "escalation_policies", type_="foreignkey"
    )
    op.drop_index("ix_escalation_policies_parent_policy_id", table_name="escalation_policies")
    op.drop_column("escalation_policies", "parent_policy_id")

    op.drop_constraint("fk_escalation_policies_tenant_id", "escalation_policies", type_="foreignkey")
    op.drop_index("ix_escalation_policies_tenant_id", table_name="escalation_policies")
    op.drop_column("escalation_policies", "tenant_id")

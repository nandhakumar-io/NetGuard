"""Tenant scoping for GitOps repo configs

Revision ID: 0100
Revises: 0099

Continues the tenancy audit started in 0095_approval_and_tenant_scoping,
which explicitly flagged GitOps as one of the remaining unscoped
routers. app.models.git_repo_config.GitRepoConfig has carried a
tenant_id column (and app.api.gitops has filtered on it) since that
audit, but the migration that was meant to add the column to the
database never actually landed -- 0097_gitops_tenant_sc was committed
as an empty, extension-less stub and never wired into the revision
chain. Every GET /gitops/repos therefore 500s ("Failed to load Git repo
configs" in the UI) because the ORM queries a column the table doesn't
have.

Closes that gap the same way 0096 did for discovery scans/schedules:
nullable + backfilled onto the "Default" tenant (NULL == MSP-staff-
authored / globally-visible, same convention throughout).
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import (
    add_column_if_missing,
    create_foreign_key_if_missing,
    create_index_if_missing,
)

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "git_repo_configs", sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    create_index_if_missing("ix_git_repo_configs_tenant_id", "git_repo_configs", ["tenant_id"])
    create_foreign_key_if_missing(
        "fk_git_repo_configs_tenant_id", "git_repo_configs", "tenants", ["tenant_id"], ["id"],
    )

    # Backfill onto "Default", same rationale as 0095/0096: existing rows
    # predate this scoping and belong to whichever tenant was "everyone"
    # before tenants existed.
    conn = op.get_bind()
    default_tenant_id = conn.execute(
        sa.text("SELECT id FROM tenants WHERE slug = 'default' LIMIT 1")
    ).scalar()
    if default_tenant_id is not None:
        conn.execute(
            sa.text("UPDATE git_repo_configs SET tenant_id = :tid WHERE tenant_id IS NULL"),
            {"tid": default_tenant_id},
        )


def downgrade() -> None:
    op.drop_constraint("fk_git_repo_configs_tenant_id", "git_repo_configs", type_="foreignkey")
    op.drop_index("ix_git_repo_configs_tenant_id", table_name="git_repo_configs")
    op.drop_column("git_repo_configs", "tenant_id")

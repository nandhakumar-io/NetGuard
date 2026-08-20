"""User extra_permissions (fine-grained capability/page permissions)

Revision ID: 0080
Revises: 0079
Create Date: 2026-08-20 00:00:02.000000

Adds `users.extra_permissions`: a comma-separated list of individually
grantable app.core.permissions.Permission keys (capability keys like
"config_management"/"network_discovery"/"logs_export", plus per-page
keys like "page:backups") an admin can hand a specific user on top of
their base `role`, without a blanket promotion to a whole other role's
surface the way `extra_roles` does. See app.core.permissions for the
registry and app.core.deps.require_roles for the enforcement side.
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("users", sa.Column("extra_permissions", sa.String(), nullable=True))


def downgrade() -> None:
    from alembic import op

    op.drop_column("users", "extra_permissions")

"""User extra_roles (fine-grained custom permissions)

Revision ID: 0079
Revises: 0078
Create Date: 2026-08-20 00:00:01.000000

Adds `users.extra_roles`: a comma-separated list of additional UserRole
values an admin can grant a specific user on top of their base `role`,
without promoting them to a whole other role. See app.core.deps.
require_roles for the enforcement side and app.api.user_management for
the admin-facing endpoint that sets this.
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("users", sa.Column("extra_roles", sa.String(), nullable=True))


def downgrade() -> None:
    from alembic import op

    op.drop_column("users", "extra_roles")

"""SSO login (Google OIDC)

Revision ID: 0059
Revises: 0058
Create Date: 2026-08-14 00:00:00.000000

Adds SSO identity columns to `users` so an org can gate onboarding
behind their IdP instead of local email/password. `hashed_password`
becomes nullable -- an SSO-only user never sets a NetGuard password,
and forcing one would just mean a second, weaker credential to leak.
`sso_provider`/`sso_subject` together are the durable link back to the
IdP account (subject is the IdP's stable user id, not the email, since
email can be reassigned at some IdPs); `sso_provider` is nullable/None
for existing local accounts so this migration is a pure additive change
with no data backfill required.
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing, create_index_if_missing

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("users", sa.Column("sso_provider", sa.String(), nullable=True))
    add_column_if_missing("users", sa.Column("sso_subject", sa.String(), nullable=True))
    add_column_if_missing(
        "users",
        sa.Column("hashed_password", sa.String(), nullable=True),
        # widen the existing NOT NULL local-password column rather than
        # add a new one -- SSO users simply leave it null.
    )
    create_index_if_missing("ix_users_sso_provider_subject", "users", ["sso_provider", "sso_subject"], unique=True)


def downgrade() -> None:
    from alembic import op

    op.drop_index("ix_users_sso_provider_subject", table_name="users")
    op.drop_column("users", "sso_subject")
    op.drop_column("users", "sso_provider")
    # hashed_password is intentionally left nullable on downgrade --
    # tightening it back to NOT NULL would fail if any SSO-only users
    # were created in the meantime; a manual backfill is safer.

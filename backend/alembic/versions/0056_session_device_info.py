"""refresh token device/location info

Revision ID: 0056
Revises: 0055
Create Date: 2026-08-14 00:00:00.000000

Adds `refresh_tokens.user_agent` and `refresh_tokens.ip_address`, captured
at login/refresh time, so the Security page's "Active Sessions" list can
show which browser/OS/device each session belongs to and (best-effort)
where it's connecting from -- instead of just an opaque session id.
Both nullable/best-effort: older rows (issued before this migration) and
non-browser API clients that don't send a User-Agent simply show as
"Unknown device".
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("refresh_tokens", sa.Column("user_agent", sa.String(), nullable=True))
    add_column_if_missing("refresh_tokens", sa.Column("ip_address", sa.String(), nullable=True))


def downgrade() -> None:
    from alembic import op

    op.drop_column("refresh_tokens", "ip_address")
    op.drop_column("refresh_tokens", "user_agent")

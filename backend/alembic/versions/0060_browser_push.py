"""Browser push notifications

Revision ID: 0060
Revises: 0059
Create Date: 2026-08-14 00:00:00.000000

Adds 'browser' as a valid app.models.push_subscription.PushProvider value
so a user can register their browser (Web Push / VAPID, delivered via the
service worker at /sw.js) as a push target the same way they'd register
ntfy or Pushover -- no separate mobile app required. See
app.services.push_service._send_browser for delivery.
"""
import sqlalchemy as sa

from migration_helpers import enum_type_exists

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from alembic import op

    if not enum_type_exists("pushprovider"):
        # 0058 hasn't run yet / hasn't created the type for some reason --
        # nothing to add a value to. It will pick up 'browser' whenever it
        # does run, same as this migration would.
        return
    bind = op.get_bind()
    existing = bind.execute(
        sa.text(
            "SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid "
            "WHERE t.typname = 'pushprovider' AND e.enumlabel = 'browser'"
        )
    ).first()
    if existing:
        return
    # ALTER TYPE ... ADD VALUE cannot run inside the same transaction as a
    # subsequent statement that uses the new value, but it's fine as its
    # own statement -- and safe to run inside Alembic's per-migration
    # transaction on Postgres 12+.
    op.execute("ALTER TYPE pushprovider ADD VALUE IF NOT EXISTS 'browser'")


def downgrade() -> None:
    # Removing a value from a Postgres enum type isn't supported without
    # rebuilding the type (and any browser subscriptions using it) --
    # left as a no-op like other additive-enum-value migrations.
    pass

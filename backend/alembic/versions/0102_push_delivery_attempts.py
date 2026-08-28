"""push delivery attempts log + browser provider enum value

Revision ID: 0102
Revises: 0101
Create Date: 2026-08-28

Adds push_delivery_attempts: a durable log of every mobile/browser push
notification_service attempted (or filtered) for a PushSubscription --
success, provider failure, or held back by severity filtering -- same
motivation as 0045_webhook_delivery_attempts but for ntfy/Pushover/
browser push. See app.models.push_subscription.PushDeliveryAttempt and
app.api.push_subscriptions (GET /push-subscriptions/deliveries,
GET /push-subscriptions/{id}/deliveries).

Also widens the `pushprovider` Postgres enum to include "browser":
app.models.push_subscription.PushProvider has had a BROWSER member since
Web Push (VAPID) support was added, but 0058_push_subscriptions only ever
created the enum type with ("ntfy", "pushover") and no later migration
added the third value -- so every attempt to register a browser push
subscription has been failing at the INSERT with "invalid input value
for enum pushprovider: browser" ever since.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op
from migration_helpers import create_index_if_missing, create_table_if_missing

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside the transaction block
    # Alembic normally wraps each migration in -- Postgres requires it
    # to run outside of one. autocommit_block() steps outside that
    # transaction for just this statement. IF NOT EXISTS makes it safe
    # to re-run (matches this file's other create_*_if_missing calls).
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE pushprovider ADD VALUE IF NOT EXISTS 'browser'")

    create_table_if_missing(
        "push_delivery_attempts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "push_subscription_id", UUID(as_uuid=True),
            sa.ForeignKey("push_subscriptions.id"), nullable=False,
        ),
        sa.Column("event", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=True),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("skipped", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("skip_reason", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    create_index_if_missing(
        "ix_push_delivery_attempts_push_subscription_id",
        "push_delivery_attempts", ["push_subscription_id"],
    )
    create_index_if_missing(
        "ix_push_delivery_attempts_attempted_at",
        "push_delivery_attempts", ["attempted_at"],
    )


def downgrade() -> None:
    from migration_helpers import drop_index_if_exists

    drop_index_if_exists("ix_push_delivery_attempts_attempted_at", table_name="push_delivery_attempts")
    drop_index_if_exists("ix_push_delivery_attempts_push_subscription_id", table_name="push_delivery_attempts")
    op.drop_table("push_delivery_attempts")
    # Postgres has no ALTER TYPE ... DROP VALUE -- the 'browser' enum
    # value is intentionally left in place on downgrade (matches how a
    # widened enum would be handled if this were still in active use
    # elsewhere; dropping the whole type back to 2 values would require
    # rewriting every existing row first).

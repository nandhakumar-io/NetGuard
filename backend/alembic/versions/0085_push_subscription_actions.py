"""push subscription action buttons

Revision ID: 0085
Revises: 0084
Create Date: 2026-08-20

Adds include_actions to push_subscriptions so a mobile push can carry
one-tap response actions (acknowledge, escalate, run runbook) alongside
the alert itself, same idea as 0084 for webhooks but for ntfy/Pushover/
browser pushes. See app.models.push_subscription.PushSubscription and
app.services.push_service.
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("push_subscriptions", sa.Column("include_actions", sa.String(), nullable=True))


def downgrade() -> None:
    from alembic import op

    op.drop_column("push_subscriptions", "include_actions")

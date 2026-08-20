"""Push notification support (ntfy, Pushover) on webhook_endpoints

Revision ID: 0082
Revises: 0081
Create Date: 2026-08-20 00:00:04.000000

Extends the existing WebhookEndpoint table (already fans out Alert
Center events to Slack/Teams/Telegram/generic webhooks -- see
app.services.notification_service) with two push-notification-only
columns rather than a new table, since push delivery reuses the same
enabled/events/name/delivery-log machinery every other webhook type
already has:

- pushover_user_key: Pushover's target user/group key. Pushover's API
  needs a user key + an app API token on every send; the token is
  stored in the existing `secret` column (already meant for a
  per-endpoint credential) instead of adding a redundant one.
- pushover_priority: optional per-endpoint override (-2..2) for
  Pushover's priority field; NULL means "derive from alert severity"
  (see notification_service.deliver_webhook).

ntfy needs no new columns: `url` is the full topic URL and `secret`
(shared with Pushover's token use above) doubles as an optional
Bearer auth token for protected topics.
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("webhook_endpoints", sa.Column("pushover_user_key", sa.String(), nullable=True))
    add_column_if_missing("webhook_endpoints", sa.Column("pushover_priority", sa.Integer(), nullable=True))


def downgrade() -> None:
    from alembic import op

    op.drop_column("webhook_endpoints", "pushover_priority")
    op.drop_column("webhook_endpoints", "pushover_user_key")

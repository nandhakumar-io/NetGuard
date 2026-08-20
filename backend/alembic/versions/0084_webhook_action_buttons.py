"""webhook action buttons

Revision ID: 0084
Revises: 0083
Create Date: 2026-08-20

Adds include_actions and default_runbook_id to webhook_endpoints so an
outbound alert delivery can carry response actions (acknowledge,
escalate, run runbook) alongside the plain notification -- Slack/Teams
render these as real interactive buttons, Telegram as an inline
keyboard, and generic endpoints get an "actions" array of deep links in
the JSON body. See app.models.webhook.WebhookEndpoint and
app.services.notification_service._build_webhook_payload.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from migration_helpers import add_column_if_missing

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing("webhook_endpoints", sa.Column("include_actions", sa.Text(), nullable=True))
    add_column_if_missing(
        "webhook_endpoints",
        sa.Column(
            "default_runbook_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("alert_runbooks.id"),
            nullable=True,
        ),
    )


def downgrade():
    from alembic import op

    op.drop_column("webhook_endpoints", "default_runbook_id")
    op.drop_column("webhook_endpoints", "include_actions")

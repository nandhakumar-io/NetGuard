"""webhook delivery attempts log

Revision ID: 0045
Revises: 0044
Create Date: 2026-08-10

Adds webhook_delivery_attempts: a durable log of every outbound HTTP call
notification_service made to a WebhookEndpoint (success or failure),
plus manual retries of a failed attempt. See
app.models.webhook.WebhookDeliveryAttempt and app.api.webhooks
(GET /webhooks/{id}/deliveries, GET /webhooks/deliveries,
POST /webhooks/deliveries/{id}/retry).
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import (
    create_index_if_missing,
    create_table_if_missing,
    drop_index_if_exists,
)

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade():
    create_table_if_missing(
        "webhook_delivery_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "webhook_endpoint_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("webhook_endpoints.id"), nullable=False,
        ),
        sa.Column("event", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=True),
        sa.Column("severity", sa.String(), nullable=True),
        sa.Column("request_payload", sa.Text(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("is_retry", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "retry_of_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("webhook_delivery_attempts.id"), nullable=True,
        ),
        sa.Column("retried_by", sa.String(), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    create_index_if_missing(
        "ix_webhook_delivery_attempts_webhook_endpoint_id",
        "webhook_delivery_attempts", ["webhook_endpoint_id"],
    )
    create_index_if_missing(
        "ix_webhook_delivery_attempts_attempted_at",
        "webhook_delivery_attempts", ["attempted_at"],
    )


def downgrade():
    drop_index_if_exists("ix_webhook_delivery_attempts_attempted_at", table_name="webhook_delivery_attempts")
    drop_index_if_exists("ix_webhook_delivery_attempts_webhook_endpoint_id", table_name="webhook_delivery_attempts")
    op.drop_table("webhook_delivery_attempts")

"""Mobile push subscriptions

Revision ID: 0058
Revises: 0057
Create Date: 2026-08-14 00:00:00.000000

Adds `push_subscriptions`: per-user mobile devices registered to receive
real phone push notifications (via ntfy or Pushover -- see
app.services.push_service) for critical incidents and alert escalations,
closing the loop for on-call engineers who aren't watching a dashboard.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from migration_helpers import create_index_if_missing, create_table_if_missing

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy.dialects.postgresql import ENUM

    from alembic import op
    from migration_helpers import enum_type_exists

    if not enum_type_exists("pushprovider"):
        ENUM("ntfy", "pushover", name="pushprovider").create(op.get_bind())

    create_table_if_missing(
        "push_subscriptions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("label", sa.String(), nullable=False, server_default="My Phone"),
        sa.Column("provider", ENUM("ntfy", "pushover", name="pushprovider", create_type=False), nullable=False, server_default="ntfy"),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("include_non_critical", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_pushed_at", sa.DateTime(timezone=True), nullable=True),
    )
    create_index_if_missing("ix_push_subscriptions_user_id", "push_subscriptions", ["user_id"])


def downgrade() -> None:
    from alembic import op

    op.drop_table("push_subscriptions")
    sa.Enum(name="pushprovider").drop(op.get_bind(), checkfirst=True)

"""Create notification_settings table

app.models.notification_settings.NotificationSettings and the
/notification-settings API (surfaced as "Email (SMTP)" on the
Integrations page) have existed for a while, but no migration ever
created the backing table -- so GET /notification-settings 500s on a
missing relation and the Integrations page shows "Failed to load email
settings." for every user, admin or not. This adds the table so the
row-per-install singleton (see SETTINGS_ROW_ID) can actually be
created/read/updated.

Revision ID: 0091
Revises: 0090
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import create_table_if_missing

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_table_if_missing(
        "notification_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("smtp_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("smtp_host", sa.String(), nullable=True),
        sa.Column("smtp_port", sa.Integer(), nullable=False, server_default="587"),
        sa.Column("smtp_username", sa.String(), nullable=True),
        sa.Column("smtp_password_encrypted", sa.String(), nullable=True),
        sa.Column("smtp_from_email", sa.String(), nullable=True),
        sa.Column("smtp_use_tls", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("recipients", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("notification_settings")

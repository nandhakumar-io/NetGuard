"""config_templates table

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-03

Jinja2 config-provisioning template library (app.models.config_template
.ConfigTemplate) -- "push standard access-switch template, fill in 3
variables" instead of hand-writing/pasting CLI/XML from scratch on every
change request. See app/services/template_service.py for rendering.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "config_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("device_role", sa.String(), nullable=True),
        sa.Column("vendor", sa.String(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("variables", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_config_templates_device_role", "config_templates", ["device_role"])
    op.create_index("ix_config_templates_vendor", "config_templates", ["vendor"])


def downgrade() -> None:
    op.drop_index("ix_config_templates_vendor", table_name="config_templates")
    op.drop_index("ix_config_templates_device_role", table_name="config_templates")
    op.drop_table("config_templates")

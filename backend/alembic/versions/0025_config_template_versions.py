"""config_template_versions table + published_version_id

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-04

Template versioning/approval workflow (app.models.config_template
.ConfigTemplateVersion). Previously editing a ConfigTemplate's body
changed it in place with no history -- fine for iterating, wrong for
anything audited. This adds an immutable snapshot table
(draft -> pending_approval -> published/rejected) and a pointer on
ConfigTemplate to whichever version is currently approved.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import (
    add_column_if_missing,
    create_index_if_missing,
    create_table_if_missing,
    drop_column_if_exists,
    drop_index_if_exists,
)

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_table_if_missing(
        "config_template_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("config_templates.id"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("variables", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending_approval"),
        sa.Column("change_note", sa.Text(), nullable=True),
        sa.Column("submitted_by", sa.String(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("reviewed_by", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
    )
    create_index_if_missing("ix_config_template_versions_template_id", "config_template_versions", ["template_id"])

    add_column_if_missing(
        "config_templates",
        sa.Column("published_version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("config_template_versions.id"), nullable=True),
    )


def downgrade() -> None:
    drop_column_if_exists("config_templates", "published_version_id")
    drop_index_if_exists("ix_config_template_versions_template_id", "config_template_versions")
    op.drop_table("config_template_versions")

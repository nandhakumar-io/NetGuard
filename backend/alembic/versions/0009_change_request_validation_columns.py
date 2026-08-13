"""Automated Validation Engine result columns on change_requests (FR-5)

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-31

Adds validation_passed / validation_errors / validation_warnings to
change_requests so the most recent validation_engine.validate_syntax()
result (re-checked at both submission and approval time) is persisted and
visible in the UI/API, instead of only affecting status transitions.
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing, drop_column_if_exists

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("change_requests", sa.Column("validation_passed", sa.String(), nullable=True))
    add_column_if_missing("change_requests", sa.Column("validation_errors", sa.Text(), nullable=True))
    add_column_if_missing("change_requests", sa.Column("validation_warnings", sa.Text(), nullable=True))


def downgrade() -> None:
    drop_column_if_exists("change_requests", "validation_warnings")
    drop_column_if_exists("change_requests", "validation_errors")
    drop_column_if_exists("change_requests", "validation_passed")

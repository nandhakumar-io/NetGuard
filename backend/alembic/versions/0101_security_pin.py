"""Security PIN (step-up auth) for terminal + critical actions

Revision ID: 0101
Revises: 0100

Adds an opt-in numeric PIN, separate from the login password, that a
user can require as a second check immediately before opening a device
terminal or firing a critical action (device delete, config rollback).
See app.models.user.User.security_pin_hash / pin_required and
app.core.deps.require_pin_step_up.

Additive/nullable + a False-defaulted `pin_required` flag, so this can
never lock out an existing account: nobody is required to have a PIN,
and enforcement only turns on once a user explicitly sets one up and
opts in.
"""
import sqlalchemy as sa

from alembic import op
from migration_helpers import add_column_if_missing

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("users", sa.Column("security_pin_hash", sa.String(), nullable=True))
    add_column_if_missing("users", sa.Column("security_pin_set_at", sa.DateTime(timezone=True), nullable=True))
    add_column_if_missing(
        "users", sa.Column("pin_required", sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade() -> None:
    op.drop_column("users", "pin_required")
    op.drop_column("users", "security_pin_set_at")
    op.drop_column("users", "security_pin_hash")

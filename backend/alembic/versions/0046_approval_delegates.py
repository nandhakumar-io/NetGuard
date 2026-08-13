"""approval delegates (backup approver mapping)

Revision ID: 0047
Revises: 0046
Create Date: 2026-08-11

Adds approval_delegates: an explicit, auditable "B may act for A" mapping
scoped to one approval stage type (peer_review / manager_signoff /
admin_approval) with an optional active time window. Lets a chain
requiring, e.g., Manager Sign-off still make progress when the only
eligible Manager is unavailable, without weakening segregation of
duties -- see app.models.approval_delegate and
app.services.approval_delegate_service.
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

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade():
    create_table_if_missing(
        "approval_delegates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("delegator_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("delegate_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "stage_type",
            sa.Enum("peer_review", "manager_signoff", "admin_approval", name="approvalstagetype"),
            nullable=False,
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("delegator_id", "delegate_id", "stage_type", "id", name="uq_approval_delegate_natural"),
    )
    create_index_if_missing("ix_approval_delegates_delegator_id", "approval_delegates", ["delegator_id"])
    create_index_if_missing("ix_approval_delegates_delegate_id", "approval_delegates", ["delegate_id"])

    add_column_if_missing(
        "change_request_approval_stages",
        sa.Column("acted_on_behalf_of", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
    )


def downgrade():
    drop_column_if_exists("change_request_approval_stages", "acted_on_behalf_of")
    drop_index_if_exists("ix_approval_delegates_delegate_id", table_name="approval_delegates")
    drop_index_if_exists("ix_approval_delegates_delegator_id", table_name="approval_delegates")
    op.drop_table("approval_delegates")

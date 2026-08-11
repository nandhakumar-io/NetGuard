"""change request approval chain

Revision ID: 0046
Revises: 0045
Create Date: 2026-08-11
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import create_table_if_missing

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

def upgrade():
    create_table_if_missing(
        "change_request_approval_stages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "change_request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("change_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "stage_type",
            sa.Enum("peer_review", "manager_signoff", "admin_approval", name="approvalstagetype"),
            nullable=False,
        ),
        sa.Column("required_role", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "approved", "rejected", name="approvalstagestatus"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("acted_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("acted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
    )

def downgrade():
    op.drop_table("change_request_approval_stages")
    op.execute("DROP TYPE IF EXISTS approvalstagetype")
    op.execute("DROP TYPE IF EXISTS approvalstagestatus")

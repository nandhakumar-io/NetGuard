"""AI Configuration Analyzer: risk classification + Critical Risk dual
approval columns on change_requests (SRS 6.2 / FR-6)

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-01

Adds risk_classification (persists the analyzer's Low/Medium/Critical
label, not just the raw score) and the three columns that back the
Critical-Risk dual-approval workflow: requires_dual_approval,
first_approved_by, first_approved_at.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("change_requests", sa.Column("risk_classification", sa.String(), nullable=True))
    op.add_column(
        "change_requests",
        sa.Column("requires_dual_approval", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "change_requests",
        sa.Column("first_approved_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("change_requests", sa.Column("first_approved_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_change_requests_first_approved_by_users",
        "change_requests",
        "users",
        ["first_approved_by"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_change_requests_first_approved_by_users", "change_requests", type_="foreignkey")
    op.drop_column("change_requests", "first_approved_at")
    op.drop_column("change_requests", "first_approved_by")
    op.drop_column("change_requests", "requires_dual_approval")
    op.drop_column("change_requests", "risk_classification")
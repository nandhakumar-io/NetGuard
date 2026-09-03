"""Add OPA/Batfish/combined pre-deployment validation fields to change_requests

Revision ID: 0123
Revises: 0122
Create Date: 2026-09-02

Adds the columns change_validation_service.py writes after each run of
the OPA policy evaluation + Batfish behavioral simulation pipeline (see
backend/app/services/change_validation_service.py, opa_service.py,
batfish_service.py). Purely additive -- no existing column is touched,
renamed, or dropped, and every new column is nullable so existing rows
and existing code paths are unaffected until the new validation pipeline
actually runs for a change request.

Findings/results use JSONB rather than new child tables to match how the
rest of this migration series stores structured, per-change data.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0123"
down_revision = "0122"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("change_requests", sa.Column("policy_validation_status", sa.String(), nullable=True))
    op.add_column("change_requests", sa.Column("policy_validation_result", postgresql.JSONB(), nullable=True))
    op.add_column("change_requests", sa.Column("policy_violations", postgresql.JSONB(), nullable=True))
    op.add_column("change_requests", sa.Column("policy_warnings", postgresql.JSONB(), nullable=True))
    op.add_column("change_requests", sa.Column("policy_version", sa.String(), nullable=True))

    op.add_column("change_requests", sa.Column("batfish_validation_status", sa.String(), nullable=True))
    op.add_column("change_requests", sa.Column("batfish_snapshot_id", sa.String(), nullable=True))
    op.add_column("change_requests", sa.Column("batfish_result", postgresql.JSONB(), nullable=True))
    op.add_column("change_requests", sa.Column("batfish_findings", postgresql.JSONB(), nullable=True))
    op.add_column("change_requests", sa.Column("batfish_behavior_changes", sa.Integer(), nullable=True))

    op.add_column("change_requests", sa.Column("combined_validation_status", sa.String(), nullable=True))
    op.add_column("change_requests", sa.Column("combined_validation_score", sa.Integer(), nullable=True))
    op.add_column("change_requests", sa.Column("combined_validation_summary", sa.Text(), nullable=True))

    op.add_column("change_requests", sa.Column("validation_engine_version", sa.String(), nullable=True))
    op.add_column("change_requests", sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True))

    op.create_index(
        "ix_change_requests_combined_validation_status",
        "change_requests",
        ["combined_validation_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_change_requests_combined_validation_status", table_name="change_requests")
    for col in [
        "policy_validation_status",
        "policy_validation_result",
        "policy_violations",
        "policy_warnings",
        "policy_version",
        "batfish_validation_status",
        "batfish_snapshot_id",
        "batfish_result",
        "batfish_findings",
        "batfish_behavior_changes",
        "combined_validation_status",
        "combined_validation_score",
        "combined_validation_summary",
        "validation_engine_version",
        "validated_at",
    ]:
        op.drop_column("change_requests", col)

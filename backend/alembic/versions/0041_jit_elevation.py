"""just-in-time (JIT) role elevation

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-09

Adds:
  - jit_elevations table: temporary, time-bound requests for an elevated
    role (see app.models.jit_elevation.JitElevation). Optionally tied to
    a change_request so "elevate me to push CR #123" is auditable end to
    end, but not required -- a JIT grant can also be requested standalone
    (e.g. incident response).
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import create_index_if_missing, create_table_if_missing

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

_STATUS_ENUM = postgresql.ENUM(
    "pending", "active", "expired", "revoked", "rejected", name="jitelevationstatus", create_type=False
)


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        _STATUS_ENUM.create(bind, checkfirst=True)

    create_table_if_missing(
        "jit_elevations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("elevated_role", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("change_request_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("change_requests.id"), nullable=True),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("requested_duration_minutes", sa.Integer(), nullable=False),
        sa.Column("status", _STATUS_ENUM, nullable=False, server_default="pending"),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    create_index_if_missing("ix_jit_elevations_user_id", "jit_elevations", ["user_id"])
    create_index_if_missing("ix_jit_elevations_change_request_id", "jit_elevations", ["change_request_id"])
    create_index_if_missing("ix_jit_elevations_status", "jit_elevations", ["status"])
    create_index_if_missing("ix_jit_elevations_expires_at", "jit_elevations", ["expires_at"])


def downgrade():
    op.drop_index("ix_jit_elevations_expires_at", table_name="jit_elevations")
    op.drop_index("ix_jit_elevations_status", table_name="jit_elevations")
    op.drop_index("ix_jit_elevations_change_request_id", table_name="jit_elevations")
    op.drop_index("ix_jit_elevations_user_id", table_name="jit_elevations")
    op.drop_table("jit_elevations")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        _STATUS_ENUM.drop(bind, checkfirst=True)

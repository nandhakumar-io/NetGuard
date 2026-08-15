"""JIT: dual-approval fields, fed by the linked change request's risk

Revision ID: 0067
Revises: 0066
Create Date: 2026-08-15 00:00:00.000000

app.services.impact_simulation_service / topology_service already compute
blast radius + reachability impact for change requests, and
ChangeRequest.requires_dual_approval already reflects that (Critical Risk
and/or blast-radius threshold, see api.change_requests._dual_approval).
Until now a JIT elevation request tied to one of those change requests was
treated exactly like any other JIT request -- one admin, up to 8 hours.

Adds the same two-step approval shape ChangeRequest already uses
(first_approved_by/at, a second *different* admin required) to
JitElevation, plus requires_dual_approval/dual_approval_reason so
jit_service can set them from the linked CR's danger classification. See
jit_service.DANGER_MAX_DURATION_MINUTES for the other half (shortened
grant window) -- that doesn't need a column since it's applied to
requested_duration_minutes directly at request time.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from migration_helpers import add_column_if_missing

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "jit_elevations",
        sa.Column("requires_dual_approval", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    add_column_if_missing(
        "jit_elevations",
        sa.Column("dual_approval_reason", sa.String(), nullable=True),
    )
    add_column_if_missing(
        "jit_elevations",
        sa.Column("first_approved_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
    )
    add_column_if_missing(
        "jit_elevations",
        sa.Column("first_approved_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    from alembic import op

    op.drop_column("jit_elevations", "first_approved_at")
    op.drop_column("jit_elevations", "first_approved_by")
    op.drop_column("jit_elevations", "dual_approval_reason")
    op.drop_column("jit_elevations", "requires_dual_approval")

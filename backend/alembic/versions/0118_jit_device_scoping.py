"""JIT device/operation scoping + merge migration heads

Revision ID: 0118
Revises: 0077, 0117
Create Date: 2026-08-30

Two things in one migration:

1. Merges the two divergent heads (0077_discovered_neighbor_switchport
   and 0117_device_pinned_critical) that had accumulated -- `alembic
   upgrade head` was ambiguous without this. See /areas/netguard6.md
   "IPAM reliability -- Alembic multi-head migration issues" for the
   pre-existing symptom this was contributing to.

2. Adds device_id + scoped_operation to jit_elevations (Section 10 /
   Phase 1 finding: JIT grants were role-based and fleet-wide, not
   scoped to a device or operation). Both nullable so every existing
   row is interpreted as an unscoped (fleet-wide) grant, unchanged --
   this is additive, not a behavior change for anything already issued.
   See app.models.jit_elevation.JitElevation and
   app.device_gateway.validator.validate for where this is now enforced.

3. Separately, alembic/versions/0027_metrics_freshness_columns.py was
   deleted in this same pass: it declared the same revision id ("0027")
   as 0027_per_metric_last_success.py, but added DIFFERENTLY-NAMED
   columns to `devices` (cpu_last_success_at, ...) than the ones
   app.models.device.Device and app.services.metrics_service actually
   use (last_cpu_success_at, ...). `alembic heads` tolerated the
   duplicate id with only a UserWarning rather than failing outright,
   which is a likely root cause of prior multi-head symptoms -- which
   of the two same-numbered files "won" depended on filesystem scan
   order, not anything deterministic. The deleted file was dead code:
   nothing in the app ever read the columns it created. Any environment
   that happened to have already run the deleted file's version will
   have harmless orphaned cpu_last_success_at-style columns sitting
   unused in `devices` -- not fixed by this migration, since Alembic
   has no record of that file's revision ever having "belonged" to a
   distinct history; noted here rather than silently ignored.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0118"
down_revision = ("0077", "0117")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jit_elevations",
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "jit_elevations",
        sa.Column("scoped_operation", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_jit_elevations_device_id", "jit_elevations", ["device_id"]
    )
    op.create_foreign_key(
        "fk_jit_elevations_device_id",
        "jit_elevations",
        "devices",
        ["device_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_jit_elevations_device_id", "jit_elevations", type_="foreignkey")
    op.drop_index("ix_jit_elevations_device_id", table_name="jit_elevations")
    op.drop_column("jit_elevations", "scoped_operation")
    op.drop_column("jit_elevations", "device_id")

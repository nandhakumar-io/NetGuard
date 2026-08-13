"""add GRAY to healthcolor enum

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-04

app.models.device_metric.HealthColor gained a GRAY member --
snmp_service.compute_health_score() now returns (None, "gray") instead
of the old (100, "green") default when a device responded to SNMP
(genuinely reachable) but resolved zero of the actual health OIDs, so a
device we have no real telemetry on no longer renders as a fabricated,
fully-green 100/100.

Postgres enum types can't have a value removed or reordered without a
full rebuild, but ADD VALUE is safe and cheap; this only adds GRAY.
Note ADD VALUE cannot run inside a transaction block in older Postgres
versions, hence autocommit here.
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE healthcolor ADD VALUE IF NOT EXISTS 'GRAY'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE -- removing GRAY would
    # require rebuilding the enum type (create new type, migrate column,
    # drop old type) and rewriting any existing GRAY rows first. Left as
    # a no-op since the added value is harmless to leave in place.
    pass

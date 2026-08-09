"""per-device polling interval overrides + last_reachability_poll_at

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-09

Discovery at Scale: the SNMP/reachability sweeps previously fanned out to
every device on every beat tick regardless of any per-device cadence, which
is fine at 4 devices and a thundering-herd problem at hundreds (all devices
polled in the same instant, every tick, no way to poll a core router more
often than a rarely-changing access switch). Adds:

  - devices.snmp_poll_interval_seconds / reachability_poll_interval_seconds:
    nullable per-device overrides of the fleet-wide defaults in
    app.core.config.settings; NULL means "use the fleet default".
  - devices.last_reachability_poll_at: reachability's equivalent of the
    existing last_snmp_poll_at, needed so the sweep can tell whether a
    device's interval has actually elapsed since its last poll.

See app.tasks.run_snmp_poll_sweep_task / run_reachability_sweep_task for
how these are read.
"""
import sqlalchemy as sa

from alembic import op
from migration_helpers import add_column_if_missing

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing("devices", sa.Column("snmp_poll_interval_seconds", sa.Integer(), nullable=True))
    add_column_if_missing("devices", sa.Column("reachability_poll_interval_seconds", sa.Integer(), nullable=True))
    add_column_if_missing(
        "devices", sa.Column("last_reachability_poll_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade():
    op.drop_column("devices", "last_reachability_poll_at")
    op.drop_column("devices", "reachability_poll_interval_seconds")
    op.drop_column("devices", "snmp_poll_interval_seconds")

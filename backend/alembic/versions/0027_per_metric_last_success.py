"""add per-metric last-success timestamps to devices

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-04

devices.last_snmp_poll_at only records when a poll *attempt* last ran,
not which individual readings within that attempt actually resolved.
Adds one nullable timestamp column per health metric (cpu / memory /
interface / temperature / fan / power) so metrics_service.poll_device
can stamp each independently -- a device with fresh CPU/mem but a
table walk that's been silently failing no longer looks fully healthy
just because the composite score still computes from what did resolve.
"""
import sqlalchemy as sa

from alembic import op
from migration_helpers import add_column_if_missing

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

_COLUMNS = [
    "last_cpu_success_at",
    "last_memory_success_at",
    "last_interface_success_at",
    "last_temperature_success_at",
    "last_fan_success_at",
    "last_power_success_at",
]


def upgrade() -> None:
    for col in _COLUMNS:
        add_column_if_missing("devices", sa.Column(col, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for col in _COLUMNS:
        op.drop_column("devices", col)


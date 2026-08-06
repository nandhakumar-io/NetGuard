"""SNMP Monitoring: add raw interface counter columns to device_metrics

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-31

Context: interface_utilization_pct on DeviceMetric can only be computed as
a delta between two polls (SNMP counters are cumulative-since-boot, not
instantaneous -- see app.services.metrics_service). That means each poll
needs to persist its raw cumulative octet total + link speed so the *next*
poll has something to diff against. These two columns are that storage.
"""
import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "device_metrics" not in inspector.get_table_names():
        # Fresh install: table doesn't exist yet, 0001's create_all will
        # bring it in (with these columns already, since they're on the
        # current model) the first time this chain runs end to end.
        return

    columns = {c["name"] for c in inspector.get_columns("device_metrics")}
    if "interface_octets_total" not in columns:
        op.add_column("device_metrics", sa.Column("interface_octets_total", sa.BigInteger(), nullable=True))
    if "interface_speed_bps" not in columns:
        op.add_column("device_metrics", sa.Column("interface_speed_bps", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("device_metrics", "interface_speed_bps")
    op.drop_column("device_metrics", "interface_octets_total")

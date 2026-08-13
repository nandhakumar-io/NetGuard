"""Drop retired device_metrics / interface_metrics tables

Revision ID: 0049
Revises: 0048
Create Date: 2026-08-12

The SNMP Health Dashboard's per-poll device/interface metrics moved to
VictoriaMetrics (see app.core.vm_client) -- app.services.metrics_service
now writes/reads through vm_client instead of the DeviceMetric /
InterfaceMetric ORM models, which have been removed. This migration drops
the two Postgres tables that backed them; downgrade() recreates the
tables (empty -- history already migrated operationally, if at all,
before this ran) so the app can still be rolled back to a pre-cutover
revision if needed.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import create_table_if_missing

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "interface_metrics" in existing_tables:
        op.drop_table("interface_metrics")
    if "device_metrics" in existing_tables:
        op.drop_table("device_metrics")
    # healthcolor enum is only used by device_metrics.health_color -- safe
    # to drop now that the table is gone. Guarded the same way the rest of
    # this repo's migrations guard enum creation, since some environments
    # may already have it dropped or never created.
    bind.execute(sa.text("SAVEPOINT _drop_healthcolor"))
    try:
        bind.execute(sa.text("DROP TYPE healthcolor"))
        bind.execute(sa.text("RELEASE SAVEPOINT _drop_healthcolor"))
    except Exception:
        bind.execute(sa.text("ROLLBACK TO SAVEPOINT _drop_healthcolor"))
        bind.execute(sa.text("RELEASE SAVEPOINT _drop_healthcolor"))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SAVEPOINT _create_healthcolor"))
    try:
        bind.execute(sa.text("CREATE TYPE healthcolor AS ENUM ('GREEN', 'YELLOW', 'RED', 'GRAY')"))
        bind.execute(sa.text("RELEASE SAVEPOINT _create_healthcolor"))
    except Exception:
        bind.execute(sa.text("ROLLBACK TO SAVEPOINT _create_healthcolor"))
        bind.execute(sa.text("RELEASE SAVEPOINT _create_healthcolor"))

    create_table_if_missing(
        "device_metrics",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=False, index=True),
        sa.Column("cpu_utilization_pct", sa.Float(), nullable=True),
        sa.Column("memory_utilization_pct", sa.Float(), nullable=True),
        sa.Column("interface_utilization_pct", sa.Float(), nullable=True),
        sa.Column("interface_errors", sa.Integer(), nullable=True),
        sa.Column("temperature_celsius", sa.Float(), nullable=True),
        sa.Column("fan_status", sa.String(), nullable=True),
        sa.Column("power_supply_status", sa.String(), nullable=True),
        sa.Column("uptime_seconds", sa.Integer(), nullable=True),
        sa.Column("interface_octets_total", sa.BigInteger(), nullable=True),
        sa.Column("interface_speed_bps", sa.BigInteger(), nullable=True),
        sa.Column("health_score", sa.Integer(), nullable=True),
        sa.Column("health_color", postgresql.ENUM("GREEN", "YELLOW", "RED", "GRAY", name="healthcolor", create_type=False), nullable=True),
        sa.Column("polled_at", sa.DateTime(timezone=True), server_default=sa.func.now(), index=True),
    )
    create_table_if_missing(
        "interface_metrics",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=False, index=True),
        sa.Column("if_index", sa.String(), nullable=False, index=True),
        sa.Column("if_descr", sa.String(), nullable=True),
        sa.Column("octets_total", sa.BigInteger(), nullable=True),
        sa.Column("speed_bps", sa.BigInteger(), nullable=True),
        sa.Column("errors", sa.Integer(), nullable=True),
        sa.Column("utilization_pct", sa.Float(), nullable=True),
        sa.Column("error_delta", sa.Integer(), nullable=True),
        sa.Column("polled_at", sa.DateTime(timezone=True), server_default=sa.func.now(), index=True),
    )

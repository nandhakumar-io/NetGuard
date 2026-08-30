"""add tplink to devicevendor enum

Revision ID: 0115
Revises: 0114
Create Date: 2026-08-29

Adds "tplink" to the devicevendor Postgres enum type so Device.vendor can
actually be set to it. Without this, app.models.device.DeviceVendor.TPLINK
exists in Python but any attempt to save a Device with vendor="tplink"
fails at the DB layer with "invalid input value for enum devicevendor" --
and the already-written TP-Link CPU/memory SNMP parsing in
snmp_service.poll_health (TPLINK_OIDS / is_tplink branch) can never
actually run, since no Device row could ever carry that vendor value.

Postgres requires ADD VALUE to run outside an explicit transaction block
(it cannot be rolled back), hence op.get_bind().execute(text(...)) with
autocommit rather than the usual migration-in-a-transaction pattern.
"""
import sqlalchemy as sa

from alembic import op

revision = "0115"
down_revision = "0114"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite (used in some test setups) has no native enum type --
        # DeviceVendor.TPLINK already works there with no schema change.
        return
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block in
    # Postgres; psycopg2's autocommit isolation level is needed here.
    with op.get_bind().engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(sa.text("ALTER TYPE devicevendor ADD VALUE IF NOT EXISTS 'tplink'"))


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE -- removing an enum value
    # requires rebuilding the type, which isn't safe to do automatically
    # if any device row is already using it. No-op; TPLINK staying in the
    # enum with nothing referencing it is harmless.
    pass

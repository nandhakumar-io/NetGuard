"""ProtocolManager integration: add 'protocol_failure' to alertsource enum

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-31

Context: app.services.protocol_manager (NETCONF/RESTCONF/SSH protocol
selection + deploy/backup/restore/drift integration) now raises an Alert
with AlertSource.PROTOCOL_FAILURE whenever a protocol-level operation
fails, so operators see it in the Alert Center the same way SNMP/drift
failures already show up. A brand-new database picks up the enum value
for free (0001's create_all builds it from the live model), but a
database that already ran 0001-0004 has a Postgres enum type frozen at
whatever values existed then and needs an explicit ALTER TYPE.

ALTER TYPE ... ADD VALUE cannot run inside a transaction block on
Postgres, so this migration is marked non-transactional.
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # sqlite (used in the test suite) has no native enum type -- the
        # Python-side Enum already includes PROTOCOL_FAILURE via the model.
        return

    with op.get_context().autocommit_block():
        conn = op.get_bind()
        res = conn.execute(sa.text("SELECT 1 FROM pg_type WHERE typname = 'alertsource'")).scalar()
        if res:
            op.execute("ALTER TYPE alertsource ADD VALUE IF NOT EXISTS 'protocol_failure'")


def downgrade() -> None:
    # Removing a value from a Postgres enum type requires rebuilding the
    # type; not worth the risk for a purely additive change.
    pass
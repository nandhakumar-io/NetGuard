"""Merge heads

Revision ID: 0078
Revises: 0077, 85cec3c5accf
Create Date: 2026-08-20 00:00:00.000000

Reconciles two divergent migration branches: the main 0001..0077 chain,
and 85cec3c5accf ("add_telemetry_tables"), which was written with
`down_revision = "0006"` instead of the actual tip at the time -- so it
branched off an old point in history instead of extending the chain,
leaving the database with two heads.

This is the same class of bug as the duplicate revision "0027" found and
fixed on Aug 14 (see backup/dashboard work that day): a plain `alembic
upgrade head` fails outright with "Multiple heads are present" once a
second head exists, so *no* migration after whichever one runs last in
practice gets applied at all until this is merged -- worth checking
`alembic heads` on the live database now that this is fixed, since any
migration written after 85cec3c5accf was introduced may never have
actually run.

No schema changes of its own -- 85cec3c5accf's table/column/enum creation
is already idempotent-guarded (create_table_if_missing etc.), so it's
safe whether or not it already ran against this database.
"""
revision = "0078"
down_revision = ("0077", "85cec3c5accf")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

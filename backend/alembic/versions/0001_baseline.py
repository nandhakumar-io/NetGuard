"""baseline: adopt alembic on top of the old create_all()-managed schema

Revision ID: 0001
Revises:
Create Date: 2026-07-31

Context: app/main.py used to call `Base.metadata.create_all(bind=engine)`
on every startup as a "prototype convenience" (see its docstring). That
call only ever creates *missing tables* -- it never adds a column to a
table that already exists. So when `mfa_secret` / `mfa_enabled` were added
to the User model, any database that already had a `users` table from an
earlier boot silently kept the old, narrower schema, which is exactly the
`column users.mfa_secret does not exist` crash on login.

This migration is written to be safe to run against either:
  (a) a completely fresh database (nothing exists yet), or
  (b) an existing database that was previously managed by create_all()
      and may be missing columns that were added to models afterwards.

For (a) it creates every table via the current models (equivalent to what
create_all used to do). For (b) it leaves existing tables alone and only
adds the specific columns known to have drifted, guarded by an inspector
check so it never fails on a column that's already there.
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # Fresh-install path: create any table that doesn't exist yet, exactly
    # as the app's models define it today (this is what create_all() did,
    # just made idempotent and repeatable instead of implicit/on every boot).
    #
    # IMPORTANT: `Base.metadata` reflects the CURRENT codebase, not the
    # schema as it stood when this migration was written -- so create_all()
    # here would also create every table added by migrations *after* this
    # one, before they get a chance to run. On a genuinely fresh database
    # that means this step and (e.g.) 85cec3c5accf both try to create
    # `alerts`, and the second one fails with "type already exists".
    #
    # Tables below are excluded because a later migration in this chain
    # already owns creating them via its own op.create_table(...). Any
    # future migration that adds `op.create_table("some_new_table", ...)`
    # must add "some_new_table" to this set too, or it will collide with
    # this baseline on fresh installs the same way.
    OWNED_BY_LATER_MIGRATIONS = {
        "config_drifts",         # 0002_config_drift.py
        "alerts",                # 85cec3c5accf_add_telemetry_tables.py
        "device_metrics",        # 85cec3c5accf_add_telemetry_tables.py
        "protocol_operations",   # 85cec3c5accf_add_telemetry_tables.py
        "deployment_logs",       # 85cec3c5accf_add_telemetry_tables.py
    }

    from app.core.database import Base

    baseline_tables = [
        table for name, table in Base.metadata.tables.items() if name not in OWNED_BY_LATER_MIGRATIONS
    ]
    Base.metadata.create_all(bind=bind, checkfirst=True, tables=baseline_tables)

    # Existing-install path: patch known schema drift on tables that
    # already existed before this migration was introduced.
    inspector = sa.inspect(bind)
    if "users" in inspector.get_table_names():
        existing_columns = {col["name"] for col in inspector.get_columns("users")}

        if "mfa_secret" not in existing_columns:
            op.add_column("users", sa.Column("mfa_secret", sa.String(), nullable=True))

        if "mfa_enabled" not in existing_columns:
            op.add_column(
                "users",
                sa.Column("mfa_enabled", sa.String(), nullable=True, server_default="false"),
            )


def downgrade() -> None:
    # Deliberately a no-op: this migration's job is to reconcile drift, not
    # to be a clean reversible step. Dropping columns/tables here would be
    # far more dangerous than the drift it fixes.
    pass
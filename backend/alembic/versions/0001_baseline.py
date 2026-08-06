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
    # This deliberately does NOT try to exclude tables that later
    # migrations (0002_config_drift.py, 85cec3c5accf_add_telemetry_tables.py)
    # also create -- an earlier version of this migration tried filtering
    # `Base.metadata` down to a table subset, which turned out to be a
    # dead end for two independent reasons, both confirmed against a real
    # Postgres instance:
    #   1. Passing a filtered `tables=` list to create_all() only skips
    #      creating the *tables* you excluded, not standalone PostgreSQL
    #      ENUM types used by those tables' columns -- SQLAlchemy treats
    #      ENUM types as metadata-scoped, created regardless of which
    #      tables you actually asked for. Even fully detaching the Table
    #      object via `MetaData.remove()` didn't stop its column's Enum
    #      type from still being created.
    #   2. Creating tables one-by-one (to dodge #1) loses create_all()'s
    #      automatic topological sort, which also breaks: this schema has
    #      a genuine circular FK (change_requests.rollback_snapshot_id ->
    #      config_snapshots.id and config_snapshots.change_request_id ->
    #      change_requests.id) that only create_all()'s built-in
    #      two-phase/ALTER-based handling resolves correctly.
    #
    # Instead, the *later* migrations that also create these tables guard
    # themselves with an inspector check (see 0002_config_drift.py's
    # `if "config_drifts" in inspector.get_table_names(): return` and the
    # equivalent `existing_tables` check in 85cec3c5accf) and skip their
    # own op.create_table(...) when this step already created it. That
    # puts the idempotency where it belongs -- on the migration that would
    # otherwise collide -- without fighting SQLAlchemy's metadata/DDL
    # internals here.
    from app.core.database import Base

    Base.metadata.create_all(bind=bind, checkfirst=True)

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

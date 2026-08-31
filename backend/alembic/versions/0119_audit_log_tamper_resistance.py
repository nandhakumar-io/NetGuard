"""Audit log tamper resistance (Section 13)

Revision ID: 0119
Revises: 0118
Create Date: 2026-08-30

Phase 1 finding: audit_logs was "immutable" only by convention --
app/services/audit_service.py's docstring says "never update or delete
... at the application layer," but nothing enforced that below the
application layer. An RCE inside the `api` container (the exact threat
this whole engagement is scoped around) could run an arbitrary
`UPDATE audit_logs SET ...` or `DELETE FROM audit_logs` through the
same DB connection every normal request uses, and rewrite or erase its
own tracks with no trace.

This migration adds two independent layers, since either one alone has
a bypass:

1. A DB-level trigger that rejects UPDATE (except to change_request_id,
   the one legitimate mutation -- see app/api/devices.py's FK-detach on
   device delete, which must keep working) and rejects DELETE outright.
   This stops an attacker who only has the app's normal DB role/
   credentials -- which is the RCE-in-api scenario -- even though they
   can otherwise run arbitrary SQL through that connection.

2. A hash chain (`prev_hash` / `record_hash`), computed server-side in
   a BEFORE INSERT trigger from the row's own immutable fields plus the
   previous row's hash. This does NOT stop a Postgres superuser (who
   can drop the trigger and rewrite history, chain and all) -- nothing
   at the DB layer can, by definition. What it buys: if a row's
   *content* is altered by any path that skips both the app layer and
   this trigger (e.g. `ALTER TABLE ... DISABLE TRIGGER` followed by a
   manual row edit, or restoring a doctored logical dump), recomputing
   the chain against stored hashes will show exactly where it breaks --
   detection, not prevention, for that residual case. Documented, not
   claimed to be solved: see the trigger-disable caveat below and the
   Phase 9 threat-model entry for T18.
"""
import sqlalchemy as sa

from alembic import op

revision = "0119"
down_revision = "0118"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_logs", sa.Column("seq", sa.BigInteger(), nullable=True))
    op.add_column("audit_logs", sa.Column("prev_hash", sa.String(length=64), nullable=True))
    op.add_column("audit_logs", sa.Column("record_hash", sa.String(length=64), nullable=True))

    op.execute("CREATE SEQUENCE IF NOT EXISTS audit_logs_seq_seq")
    op.execute(
        "ALTER TABLE audit_logs ALTER COLUMN seq SET DEFAULT nextval('audit_logs_seq_seq')"
    )
    # Backfill seq for any pre-existing rows in insertion order, then make
    # it NOT NULL / unique so it's a reliable strict ordering independent
    # of created_at (which has second-level resolution and could tie).
    op.execute(
        """
        WITH ordered AS (
            SELECT id, row_number() OVER (ORDER BY created_at, id) AS rn
            FROM audit_logs
        )
        UPDATE audit_logs
        SET seq = ordered.rn
        FROM ordered
        WHERE audit_logs.id = ordered.id AND audit_logs.seq IS NULL
        """
    )
    op.execute(
        "SELECT setval('audit_logs_seq_seq', COALESCE((SELECT MAX(seq) FROM audit_logs), 0) + 1, false)"
    )
    op.alter_column("audit_logs", "seq", nullable=False)
    op.create_unique_constraint("uq_audit_logs_seq", "audit_logs", ["seq"])

    # --- Hash-chaining trigger (fires on INSERT only; UPDATE is blocked
    # separately below before it could ever touch a hash column anyway) ---
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_logs_set_hash_chain() RETURNS trigger AS $$
        DECLARE
            last_hash text;
        BEGIN
            SELECT record_hash INTO last_hash
            FROM audit_logs
            ORDER BY seq DESC
            LIMIT 1;

            NEW.prev_hash := COALESCE(last_hash, repeat('0', 64));
            NEW.record_hash := encode(
                digest(
                    NEW.prev_hash || '|' ||
                    COALESCE(NEW.seq::text, '') || '|' ||
                    COALESCE(NEW.actor, '') || '|' ||
                    COALESCE(NEW.action, '') || '|' ||
                    COALESCE(NEW.result, '') || '|' ||
                    COALESCE(NEW.device_hostname, '') || '|' ||
                    COALESCE(NEW.detail, '') || '|' ||
                    COALESCE(NEW.tenant_id::text, '') || '|' ||
                    COALESCE(NEW.created_at::text, now()::text),
                    'sha256'
                ),
                'hex'
            );
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    # pgcrypto provides digest(); create it if this DB doesn't already have it.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_audit_logs_hash_chain ON audit_logs;
        CREATE TRIGGER trg_audit_logs_hash_chain
        BEFORE INSERT ON audit_logs
        FOR EACH ROW
        EXECUTE FUNCTION audit_logs_set_hash_chain();
        """
    )

    # --- Immutability trigger: block UPDATE (except change_request_id
    # detach) and block DELETE entirely, at the database layer. This is
    # the layer that actually matters for "a compromised /api container
    # must not be able to silently modify historical audit records" --
    # everything above is best-effort detection, this is prevention. ---
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_logs_prevent_tamper() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'audit_logs is append-only: DELETE is not permitted (row id %)', OLD.id;
            END IF;

            IF TG_OP = 'UPDATE' THEN
                -- Only change_request_id may change (device-delete FK
                -- detach, see app/api/devices.py). Every other column,
                -- including the hash-chain columns themselves, must be
                -- byte-identical to the original row.
                IF NEW.id                 IS DISTINCT FROM OLD.id
                   OR NEW.actor           IS DISTINCT FROM OLD.actor
                   OR NEW.action          IS DISTINCT FROM OLD.action
                   OR NEW.device_hostname IS DISTINCT FROM OLD.device_hostname
                   OR NEW.result          IS DISTINCT FROM OLD.result
                   OR NEW.detail          IS DISTINCT FROM OLD.detail
                   OR NEW.tenant_id       IS DISTINCT FROM OLD.tenant_id
                   OR NEW.created_at      IS DISTINCT FROM OLD.created_at
                   OR NEW.seq             IS DISTINCT FROM OLD.seq
                   OR NEW.prev_hash       IS DISTINCT FROM OLD.prev_hash
                   OR NEW.record_hash     IS DISTINCT FROM OLD.record_hash
                THEN
                    RAISE EXCEPTION
                        'audit_logs is append-only: only change_request_id may be updated (row id %)', OLD.id;
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_audit_logs_prevent_tamper ON audit_logs;
        CREATE TRIGGER trg_audit_logs_prevent_tamper
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW
        EXECUTE FUNCTION audit_logs_prevent_tamper();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_prevent_tamper ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS audit_logs_prevent_tamper()")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_hash_chain ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS audit_logs_set_hash_chain()")
    op.drop_constraint("uq_audit_logs_seq", "audit_logs", type_="unique")
    op.drop_column("audit_logs", "record_hash")
    op.drop_column("audit_logs", "prev_hash")
    op.drop_column("audit_logs", "seq")
    op.execute("DROP SEQUENCE IF EXISTS audit_logs_seq_seq")

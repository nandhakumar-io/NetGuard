-- Runs once, automatically, on first cluster initialization only (see
-- docker-entrypoint-initdb.d semantics of the postgres image) -- NOT on
-- every container restart, so this never fights with data that already
-- exists.
--
-- Section 18 (DB security) / closes the residual risk noted against
-- migration 0119 (audit_logs tamper-resistance trigger): until now,
-- every NetGuard component -- api, every Celery worker, and the
-- one-shot `migrate` service -- connected as the SAME Postgres role
-- (${POSTGRES_USER}, "netguard" by default), which OWNS every table it
-- created via `alembic upgrade head`. Table ownership includes the
-- right to `ALTER TABLE ... DISABLE TRIGGER`, so that one role could
-- always disable audit_logs_prevent_tamper and rewrite history --
-- meaning a compromised `api` process (this whole engagement's threat
-- model) had that right too, migration 0119's trigger notwithstanding.
--
-- Fix: split into two roles.
--   netguard        (${POSTGRES_USER}) -- owner/migrator. Only the
--                    one-shot `migrate` service (see docker-compose's
--                    `migrate` service and backend/entrypoint.sh) uses
--                    this role, to run `alembic upgrade head` and the
--                    grants script below. Nothing else connects as it.
--   netguard_app     -- runtime role. `api` and every worker
--                    (beat/poller/deployer/firmware/worker/notifier/
--                    collector/syslog-collector/flow-collector) connect
--                    as this instead. It owns nothing, so it has no
--                    ALTER/DROP/TRIGGER-management rights on any table
--                    by construction -- not because something remembered
--                    to revoke them, but because a non-owning,
--                    non-superuser role never has them in Postgres
--                    unless separately granted (and this script never
--                    grants them). See grants.sql for the specific
--                    DML privileges it does get, including the
--                    audit_logs-specific restriction (no UPDATE/DELETE
--                    even though it has UPDATE/DELETE on every other
--                    table) that backs up migration 0119's trigger with
--                    a second, independent layer.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'netguard_app') THEN
        CREATE ROLE netguard_app
            LOGIN
            PASSWORD :'netguard_app_password'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT;
    END IF;
END
$$;
#!/bin/bash
# Runs once, automatically, on first cluster initialization only (see
# docker-entrypoint-initdb.d semantics of the official postgres image) --
# NOT on every container restart, so this never fights with data that
# already exists. Shell (not plain .sql) so it can read
# NETGUARD_APP_DB_PASSWORD from the environment rather than hardcoding it.
#
# Section 18 (DB security) / closes the residual risk noted against
# migration 0119 (audit_logs tamper-resistance trigger): every NetGuard
# component -- api, every Celery worker, and the one-shot `migrate`
# service -- used to connect as the SAME Postgres role
# (${POSTGRES_USER}, "netguard" by default), which OWNS every table it
# created via `alembic upgrade head`. Table ownership includes the right
# to `ALTER TABLE ... DISABLE TRIGGER`, so that one role could always
# disable audit_logs_prevent_tamper and rewrite history -- meaning a
# compromised `api` process (this whole engagement's threat model) had
# that right too, migration 0119's trigger notwithstanding.
#
# Fix: split into two roles.
#   netguard (${POSTGRES_USER})  -- owner/migrator. Only the one-shot
#     `migrate` service (docker-compose's `migrate` service +
#     backend/entrypoint.sh) connects as this, to run
#     `alembic upgrade head` and backend/alembic/grants.sql. Nothing
#     else uses it.
#   netguard_app                 -- runtime role, created here. `api`
#     and every worker (beat/poller/deployer/firmware/worker/notifier/
#     collector/syslog-collector/flow-collector) connect as this
#     instead. It owns nothing, so it has no ALTER/DROP/TRIGGER-
#     management rights on any table by construction -- not because
#     something remembered to revoke them, but because a non-owning,
#     non-superuser Postgres role never has them unless separately
#     granted, and nothing here grants them. See grants.sql for the
#     specific DML privileges it does get, including an audit_logs-
#     specific restriction (no UPDATE/DELETE, even though it has
#     UPDATE/DELETE on every other application table) that backs up
#     migration 0119's trigger with a second, independent layer -- so
#     even if the trigger were somehow bypassed, this role still
#     couldn't issue the UPDATE/DELETE for the trigger to block.

set -e

: "${NETGUARD_APP_DB_PASSWORD:?NETGUARD_APP_DB_PASSWORD must be set for db-init to create the netguard_app role}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'netguard_app') THEN
            CREATE ROLE netguard_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
        END IF;
    END
    \$\$;

    ALTER ROLE netguard_app WITH PASSWORD '${NETGUARD_APP_DB_PASSWORD}';
EOSQL
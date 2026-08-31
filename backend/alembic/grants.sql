-- Run by the `migrate` service, as the owning role (${POSTGRES_USER}),
-- immediately after `alembic upgrade head` (see backend/entrypoint.sh).
-- Idempotent -- safe to run after every migration, including ones that
-- add no new tables.
--
-- Grants the runtime role (netguard_app, created by
-- docker/db-init/01-create-app-role.sh) exactly the DML it needs and
-- nothing else. In particular:
--   * No CREATE/ALTER/DROP/TRIGGER rights anywhere -- netguard_app owns
--     no objects, and ownership (not a GRANT) is what carries DDL
--     rights in Postgres, so there is nothing to revoke here; the
--     absence is structural, not a maintained list.
--   * audit_logs specifically gets SELECT/INSERT only, not
--     UPDATE/DELETE, even though every other table gets full DML. This
--     is deliberately redundant with migration 0119's
--     audit_logs_prevent_tamper trigger -- that trigger stops UPDATE/
--     DELETE regardless of role, but this grant means the runtime role
--     the API and workers actually use couldn't even attempt the
--     statement in the first place. Two independent layers so a bug or
--     bypass in either one doesn't fall through to the other.

GRANT USAGE ON SCHEMA public TO netguard_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO netguard_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO netguard_app;

-- Tighten audit_logs beyond the blanket grant above.
REVOKE UPDATE, DELETE ON audit_logs FROM netguard_app;

-- So tables/sequences created by FUTURE migrations (run by this same
-- owning role) are automatically granted to netguard_app too, without
-- this script needing to be updated every time a migration adds a
-- table. audit_logs already exists, so this doesn't retroactively
-- widen it -- the REVOKE above stays in effect for it; only genuinely
-- new tables get the broader default.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO netguard_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO netguard_app;
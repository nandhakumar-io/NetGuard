#!/bin/sh
# Applies any pending Alembic migrations before starting the process this
# container was told to run (uvicorn, celery worker, or celery beat).
# This is the single place schema changes get applied -- replaces the old
# `Base.metadata.create_all()` on every FastAPI startup, which is what let
# `users.mfa_secret` drift out of sync with the deployed DB in the first
# place (create_all only creates missing tables, never adds a column to
# one that already exists).
set -e

# SKIP_MIGRATIONS=true is set on scaled-out containers (api replicas,
# collector, worker) once a dedicated one-shot `migrate` service exists in
# docker-compose -- otherwise N api replicas + the collector + the worker
# all race to `alembic upgrade head` against the same DB concurrently on
# every `docker compose up`/restart, which is unsafe (Alembic's version
# bookkeeping isn't designed for concurrent upgraders). Defaults to unset
# (migrations run) so a plain `docker compose up` on a single-instance
# stack, or the `migrate` service itself, keeps working with no extra
# config.
if [ "${SKIP_MIGRATIONS:-false}" = "true" ]; then
  echo "SKIP_MIGRATIONS=true -- assuming the migrate service already applied migrations."
else
  echo "Applying database migrations..."
  alembic upgrade head

  # Re-sync netguard_app's grants after every migration (Section 18 /
  # 0119's owner-vs-runtime-role split -- see grants.sql). Only the
  # `migrate` service runs this block (SKIP_MIGRATIONS is unset only for
  # it), and only `migrate` connects to Postgres as the owning role that
  # can run GRANT/ALTER DEFAULT PRIVILEGES in the first place -- api and
  # the workers connect as netguard_app itself, which couldn't run this
  # script even if it tried. Uses DATABASE_URL's own credentials, so it
  # runs as whatever role `migrate` is configured with, without needing
  # a second connection string.
  echo "Syncing netguard_app grants..."
  python3 - <<'PYEOF'
import os
import psycopg2

url = os.environ["DATABASE_URL"].replace("postgresql+psycopg2://", "postgresql://", 1)
with psycopg2.connect(url) as conn:
    conn.autocommit = True
    with conn.cursor() as cur, open("/app/alembic/grants.sql") as f:
        cur.execute(f.read())
print("netguard_app grants synced.")
PYEOF
fi

exec "$@"
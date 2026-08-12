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
fi

exec "$@"
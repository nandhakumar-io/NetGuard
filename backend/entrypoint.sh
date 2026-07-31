#!/bin/sh
# Applies any pending Alembic migrations before starting the process this
# container was told to run (uvicorn, celery worker, or celery beat).
# This is the single place schema changes get applied -- replaces the old
# `Base.metadata.create_all()` on every FastAPI startup, which is what let
# `users.mfa_secret` drift out of sync with the deployed DB in the first
# place (create_all only creates missing tables, never adds a column to
# one that already exists).
set -e

echo "Applying database migrations..."
alembic upgrade head

exec "$@"

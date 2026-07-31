#!/bin/sh
# Entrypoint for the dedicated `migrate` service. Runs once, applies
# migrations, exits. Every other service (backend, worker, beat) depends
# on this one completing successfully before it starts, so `alembic
# upgrade head` only ever executes from a single process -- no more races
# between containers creating the same enum/table at the same time.
set -e
echo "Applying database migrations..."
alembic upgrade head
echo "Migrations applied."

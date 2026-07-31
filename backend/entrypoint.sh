#!/bin/sh
# Starts whatever process this container was told to run (uvicorn, celery
# worker, celery beat). Does NOT run migrations itself -- see
# entrypoint-migrate.sh for that.
#
# Migrations used to run here, in every container's entrypoint. That's
# what caused the `type "driftseverity" already exists` crash: backend
# and worker start at the same time, both ran `alembic upgrade head`
# concurrently, and both tried to CREATE TYPE for the same not-yet-existing
# enum in the same instant -- a check-then-act race across processes that
# `checkfirst=True` can't close (the check and the create aren't atomic
# between two separate connections). Migrations now run exactly once, from
# a dedicated one-shot `migrate` service that every other service waits on
# (docker-compose.yml: `depends_on: migrate: condition: service_completed_successfully`).
set -e
exec "$@"

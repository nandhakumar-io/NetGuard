"""Discovery scan celery_task_id (for real scan cancellation)

Revision ID: 0081
Revises: 0080
Create Date: 2026-08-20 00:00:03.000000

Adds `discovery_scans.celery_task_id` so POST
/discovery/scans/{id}/cancel (see app.api.network_discovery) can issue a
real celery_app.control.revoke(task_id, terminate=True) against the
in-flight worker task, not just flip the DB row -- a scan already mid-sweep
would never notice a DB-only status change since
app.services.network_discovery_service.run_scan didn't previously check
for cancellation at all, which is what let a scan "run forever" with no
way to actually stop it.
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("discovery_scans", sa.Column("celery_task_id", sa.String(), nullable=True))


def downgrade() -> None:
    from alembic import op

    op.drop_column("discovery_scans", "celery_task_id")

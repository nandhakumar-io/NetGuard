"""JIT: track whether an "expiring soon" notification has already fired

Revision ID: 0068
Revises: 0067
Create Date: 2026-08-16 00:00:00.000000

jit_service previously only ever mutated `status` at expiry time, and only
lazily (mark_expired_elevations, called from list/metrics endpoints -- see
that function's docstring). Nobody was ever told a grant was *about* to
lapse mid-task, and nobody was told when one actually did lapse either --
both are addressed by app.services.jit_service.sweep_expiry_notifications,
run periodically off Celery beat (see celery_app "jit-expiry-notify-sweep").

expiry_warning_sent_at records when the "expiring soon" notification fired
for this row, so a periodic sweep (which may tick every couple of minutes)
never re-sends the same warning on every tick -- same
"stage tracked on the row so repeated sweeps don't spam" shape as
ChangeRequest.sla_last_notified_stage / approval_sla_notifier_service.
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "jit_elevations",
        sa.Column("expiry_warning_sent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    from alembic import op

    op.drop_column("jit_elevations", "expiry_warning_sent_at")

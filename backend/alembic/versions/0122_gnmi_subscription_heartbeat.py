"""gNMI subscription DB heartbeat (fixes GET /gnmi/status after Gateway relocation)

Revision ID: 0122
Revises: 0121
Create Date: 2026-08-30

The gNMI streaming supervisor moved from the `api` process into
`device-gateway` as part of the 0121 device-credential key re-scoping
(api/main.py's lifespan no longer starts it; app/device_gateway/main.py's
run() does). GET /gnmi/status (app/api/gnmi.py) previously read the
supervisor's in-process singleton via gnmi_service.get_supervisor() --
that only worked because the supervisor and the API route shared a
process. Now they're in different containers, so that call always
returns None from `api`, and the endpoint silently reported every
device as not-subscribed regardless of actual state.

Adds a DB-backed heartbeat instead: GnmiSupervisor's reconcile loop
(device-gateway) writes gnmi_subscription_active/heartbeat_at for every
gNMI-enabled device on each tick; the API route reads these columns
instead of the in-process singleton, treating a stale heartbeat (no
update within 2x the reconcile interval -- Gateway down/unreachable) as
not-subscribed rather than trusting a possibly-ancient active flag.
"""
import sqlalchemy as sa

from alembic import op

revision = "0122"
down_revision = "0121"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("gnmi_subscription_active", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "devices",
        sa.Column("gnmi_subscription_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("devices", "gnmi_subscription_heartbeat_at")
    op.drop_column("devices", "gnmi_subscription_active")

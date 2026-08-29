"""wireless client sessions (sticky-client detection)

Revision ID: 0110
Revises: 0109
Create Date: 2026-08-29

Adds wireless_client_sessions -- one row per (controller, client MAC),
tracking which AP each wireless client is currently associated to and
since when. Backs app.services.wireless_service.get_sticky_clients,
which flags clients that have dwelled a long time on a congested AP
instead of roaming, using data (bsnMobileStationAPMacAddr) the poll
already walks for the per-band client split but previously discarded.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0110"
down_revision = "0109"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wireless_client_sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "controller_device_id",
            UUID(as_uuid=True),
            sa.ForeignKey("devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_mac", sa.String(), nullable=False),
        sa.Column("ap_index", sa.String(), nullable=True),
        sa.Column("ap_mac_address", sa.String(), nullable=True),
        sa.Column("band", sa.String(), nullable=True),
        sa.Column("first_seen_on_ap", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("controller_device_id", "client_mac", name="uq_client_session_controller_mac"),
    )
    op.create_index(
        "ix_wireless_client_sessions_controller_device_id",
        "wireless_client_sessions",
        ["controller_device_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_wireless_client_sessions_controller_device_id", table_name="wireless_client_sessions")
    op.drop_table("wireless_client_sessions")

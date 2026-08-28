"""add wireless AP and SSID snapshot tables

Revision ID: 0104
Revises: 0103
Create Date: 2026-08-28

Adds two tables that store the most-recent SNMP poll snapshot for
wireless infrastructure managed through a Cisco AireOS WLC (or any
compatible SNMP controller):

  wireless_aps   -- one row per AP per controller, keyed by
                    (controller_device_id, ap_index).  Carries the AP
                    name, oper status, client count, and the per-band
                    client split that drives the 2.4 GHz / 5 GHz
                    utilisation widget on the Wireless page.

  wireless_ssids -- one row per SSID per controller, keyed by
                    (controller_device_id, ssid_index).  Carries the
                    SSID name, admin status, and associated client count.

Both tables are upserted on each poll cycle
(app.services.wireless_service.poll_wireless_controller) so they always
reflect the latest snapshot rather than growing unboundedly.  Historical
trending of client counts is not in scope for this iteration -- that is
a separate concern (VictoriaMetrics push) if ever needed.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op
from migration_helpers import create_table_if_missing

revision = "0104"
down_revision = "0103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_table_if_missing(
        "wireless_aps",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("controller_device_id", UUID(as_uuid=True),
                  sa.ForeignKey("devices.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("ap_index", sa.String(), nullable=False),
        # Human-readable AP name reported by the WLC (bsnAPName).
        sa.Column("ap_name", sa.String(), nullable=True),
        sa.Column("ap_model", sa.String(), nullable=True),
        sa.Column("ap_ip_address", sa.String(), nullable=True),
        # 1 = associated (up), 2 = disassociating, 3 = downloading (booting)
        sa.Column("oper_status", sa.Integer(), nullable=True),
        # Total associated client count across all radios (bsnApNumOfUsers).
        sa.Column("client_count", sa.Integer(), nullable=True),
        # Per-band client splits (best-effort; null when not available).
        sa.Column("band_2g_clients", sa.Integer(), nullable=True),
        sa.Column("band_5g_clients", sa.Integer(), nullable=True),
        sa.Column("polled_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("controller_device_id", "ap_index",
                            name="uq_wireless_ap_controller_index"),
    )

    create_table_if_missing(
        "wireless_ssids",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("controller_device_id", UUID(as_uuid=True),
                  sa.ForeignKey("devices.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("ssid_index", sa.String(), nullable=False),
        sa.Column("ssid_name", sa.String(), nullable=False),
        # 1 = enabled, 0 = disabled (bsnDot11EssAdminStatus).
        sa.Column("admin_status", sa.Integer(), nullable=True),
        # Mobile stations currently associated to this SSID across all APs.
        sa.Column("mobile_station_count", sa.Integer(), nullable=True),
        sa.Column("polled_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("controller_device_id", "ssid_index",
                            name="uq_wireless_ssid_controller_index"),
    )


def downgrade() -> None:
    op.drop_table("wireless_ssids")
    op.drop_table("wireless_aps")

"""wireless AP CRUD support (vendor, manual management, optional controller)

Revision ID: 0105
Revises: 0104
Create Date: 2026-08-29

The Wireless page previously only ever showed APs discovered by polling
a Cisco AireOS WLC over SNMP -- there was no way to add, edit, or remove
an AP by hand, and nothing but Cisco WLC-managed APs could appear at
all. This migration turns wireless_aps into a real inventory table:

  * controller_device_id becomes nullable -- a manually-added AP (e.g.
    a standalone TP-Link or Ruckus AP with no WLC/controller) doesn't
    need one.
  * vendor -- "cisco" | "aruba" | "ruckus" | "tplink" | "ubiquiti" |
    "mikrotik" | "other". Drives the vendor badge/filter and, for
    manually-managed APs, which reachability check is used.
  * mac_address, site, notes -- basic inventory fields for manually
    added APs.
  * management_ip -- separate from the SNMP-polled ap_ip_address so a
    manually-entered management address survives even if the AP is
    never polled.
  * source -- "polled" (came from an SNMP WLC poll, read-only-ish) or
    "manual" (created via the CRUD API/UI, fully editable). Existing
    rows are backfilled to "polled" since they all came from
    poll_wireless_controller.
  * created_at -- manual APs need a real creation timestamp; polled
    APs reuse polled_at for this so the column is simply always set.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op
from migration_helpers import add_column_if_missing, column_exists

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Controller becomes optional -- manually-added APs have no WLC.
    if column_exists("wireless_aps", "controller_device_id"):
        op.alter_column(
            "wireless_aps", "controller_device_id",
            existing_type=UUID(as_uuid=True), nullable=True,
        )

    add_column_if_missing(
        "wireless_aps",
        sa.Column("vendor", sa.String(), nullable=False, server_default="cisco"),
    )
    add_column_if_missing(
        "wireless_aps",
        sa.Column("mac_address", sa.String(), nullable=True),
    )
    add_column_if_missing(
        "wireless_aps",
        sa.Column("management_ip", sa.String(), nullable=True),
    )
    add_column_if_missing(
        "wireless_aps",
        sa.Column("site", sa.String(), nullable=True),
    )
    add_column_if_missing(
        "wireless_aps",
        sa.Column("notes", sa.String(), nullable=True),
    )
    add_column_if_missing(
        "wireless_aps",
        sa.Column("source", sa.String(), nullable=False, server_default="polled"),
    )
    add_column_if_missing(
        "wireless_aps",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ap_index is only meaningful for polled APs (SNMP table index); for
    # manually-created rows there's no controller table row to index
    # into, so allow it to be null and stop treating it as required.
    if column_exists("wireless_aps", "ap_index"):
        op.alter_column(
            "wireless_aps", "ap_index",
            existing_type=sa.String(), nullable=True,
        )


def downgrade() -> None:
    for col in ["vendor", "mac_address", "management_ip", "site", "notes", "source", "created_at"]:
        if column_exists("wireless_aps", col):
            op.drop_column("wireless_aps", col)

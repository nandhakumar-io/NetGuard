"""wireless_aps troubleshooting telemetry (uptime, sw version, serial,
per-radio channel/power/noise/utilization)

Revision ID: 0107
Revises: 0106
Create Date: 2026-08-29

The Wireless page previously only showed model/IP/client-count -- enough
to know an AP is up, not enough to troubleshoot it (wrong channel picked
by RRM, an AP still on last quarter's firmware, RF too noisy to hold
clients even though the AP itself reports "associated"). These columns
back the additional fields wireless_service.poll_wireless_controller now
reads from AIRESPACE-WIRELESS-MIB's bsnAPTable/bsnAPIfTable. All
nullable: a controller/firmware combination that doesn't expose one of
these just leaves it unset rather than failing the poll.
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing, drop_column_if_exists

revision = "0107"
down_revision = "0106"
branch_labels = None
depends_on = None

_NEW_COLUMNS = [
    ("ap_up_time", sa.String()),
    ("ap_software_version", sa.String()),
    ("ap_serial_number", sa.String()),
    ("channel_2g", sa.Integer()),
    ("channel_5g", sa.Integer()),
    ("tx_power_2g", sa.Integer()),
    ("tx_power_5g", sa.Integer()),
    ("noise_2g", sa.Integer()),
    ("noise_5g", sa.Integer()),
    ("channel_util_2g", sa.Integer()),
    ("channel_util_5g", sa.Integer()),
]


def upgrade() -> None:
    for name, coltype in _NEW_COLUMNS:
        add_column_if_missing("wireless_aps", sa.Column(name, coltype, nullable=True))


def downgrade() -> None:
    for name, _coltype in _NEW_COLUMNS:
        drop_column_if_exists("wireless_aps", name)

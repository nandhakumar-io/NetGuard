"""wireless SSID security mode / weak-security flag

Revision ID: 0113
Revises: 0112
Create Date: 2026-08-29

Adds security_mode + is_weak_security to wireless_ssids, populated from
bsnDot11EssWepState/bsnDot11EssWPA1Enable/bsnDot11EssWPA2Enable (see
wireless_service._classify_ssid_security). Lets an open/WEP/WPA1-TKIP
SSID surface as a "Weak SSID Security" finding next to the existing
rogue-AP detection, instead of going unnoticed.
"""
import sqlalchemy as sa

from alembic import op

revision = "0113"
down_revision = "0112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("wireless_ssids", sa.Column("security_mode", sa.String(), nullable=True))
    op.add_column("wireless_ssids", sa.Column("is_weak_security", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("wireless_ssids", "is_weak_security")
    op.drop_column("wireless_ssids", "security_mode")

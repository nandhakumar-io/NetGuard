"""standalone wireless AP own SNMP credentials

Revision ID: 0114
Revises: 0113
Create Date: 2026-08-29

Lets a manually-added (source="manual") standalone AP -- Ruckus, TP-Link
Omada, MikroTik with no WLC -- carry its own SNMP credentials instead of
requiring a duplicate Device entry at the same IP just to borrow creds
for polling. See wireless_service.build_snmp_auth_from_ap and
POST /wireless/aps/{id}/snmp-credentials. Mirrors Device's snmp_* column
shape (app.models.device.Device) so the same Fernet encrypt/decrypt via
app.core.crypto applies.
"""
import sqlalchemy as sa

from alembic import op

revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("wireless_aps", sa.Column("snmp_version", sa.String(), nullable=True))
    op.add_column("wireless_aps", sa.Column("snmp_port", sa.Integer(), nullable=True))
    op.add_column("wireless_aps", sa.Column("snmp_username", sa.String(), nullable=True))
    op.add_column("wireless_aps", sa.Column("snmp_security_level", sa.String(), nullable=True))
    op.add_column("wireless_aps", sa.Column("snmp_auth_protocol", sa.String(), nullable=True))
    op.add_column("wireless_aps", sa.Column("snmp_priv_protocol", sa.String(), nullable=True))
    op.add_column("wireless_aps", sa.Column("snmp_community_encrypted", sa.Text(), nullable=True))
    op.add_column("wireless_aps", sa.Column("snmp_auth_key_encrypted", sa.Text(), nullable=True))
    op.add_column("wireless_aps", sa.Column("snmp_priv_key_encrypted", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("wireless_aps", "snmp_priv_key_encrypted")
    op.drop_column("wireless_aps", "snmp_auth_key_encrypted")
    op.drop_column("wireless_aps", "snmp_community_encrypted")
    op.drop_column("wireless_aps", "snmp_priv_protocol")
    op.drop_column("wireless_aps", "snmp_auth_protocol")
    op.drop_column("wireless_aps", "snmp_security_level")
    op.drop_column("wireless_aps", "snmp_username")
    op.drop_column("wireless_aps", "snmp_port")
    op.drop_column("wireless_aps", "snmp_version")

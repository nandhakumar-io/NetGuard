"""IPAM: nmap OS/device-type fingerprint columns on subnet_scanned_hosts

Revision ID: 0063
Revises: 0062
Create Date: 2026-08-14 00:00:00.000000

Adds columns for an opt-in `nmap -O` fingerprinting pass, layered on top
of the plain -sn ping-sweep added in 0061. Separate migration (rather
than folding into 0061) because this is a genuinely different
capability tier: -sn works from an unprivileged container, -O needs a
raw socket (root, or CAP_NET_RAW+CAP_NET_ADMIN) and so may simply never
get used in some deployments -- see
app.services.ipam_service.fingerprint_subnet's docstring.
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("subnet_scanned_hosts", sa.Column("os_guess", sa.String(), nullable=True))
    add_column_if_missing("subnet_scanned_hosts", sa.Column("os_accuracy", sa.Integer(), nullable=True))
    add_column_if_missing("subnet_scanned_hosts", sa.Column("device_type", sa.String(), nullable=True))
    add_column_if_missing("subnet_scanned_hosts", sa.Column("mac_vendor", sa.String(), nullable=True))
    add_column_if_missing("subnet_scanned_hosts", sa.Column("fingerprinted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    from alembic import op

    op.drop_column("subnet_scanned_hosts", "fingerprinted_at")
    op.drop_column("subnet_scanned_hosts", "mac_vendor")
    op.drop_column("subnet_scanned_hosts", "device_type")
    op.drop_column("subnet_scanned_hosts", "os_accuracy")
    op.drop_column("subnet_scanned_hosts", "os_guess")

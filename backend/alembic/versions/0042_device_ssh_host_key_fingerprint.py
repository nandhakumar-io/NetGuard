"""devices.ssh_host_key_fingerprint

Revision ID: 0042
Revises: 0041
Create Date: 2026-08-10

Adds trust-on-first-use (TOFU) SSH host key pinning for the web terminal
(app.api.terminal). Previously asyncssh.connect(..., known_hosts=None)
accepted whatever host key a device presented on every connection, which
means an on-path attacker (ARP spoofing, a rogue switch port, DNS/route
manipulation) could transparently MITM the terminal session -- capturing
device credentials and full session content -- and the operator would see
nothing different in the UI. Pinning the fingerprint on first connect and
verifying it on every subsequent connect turns an undetectable MITM into
a hard connection failure with a clear "host key changed" warning.
"""
import sqlalchemy as sa

from alembic import op
from migration_helpers import add_column_if_missing

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("devices", sa.Column("ssh_host_key_fingerprint", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("devices", "ssh_host_key_fingerprint")

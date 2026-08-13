"""devices.ssh_key_auth

Revision ID: 0044
Revises: 0043
Create Date: 2026-08-10

Adds per-device SSH key-based authentication as an alternative to the
existing password-only flow (ssh_password_encrypted). Devices/orgs that
require key-based login can now set a private key (and optional
passphrase) via POST /devices/{id}/ssh-credentials and select
ssh_auth_method="key"; the web terminal (app.api.terminal) presents
whichever credential ssh_auth_method points at. Existing rows default to
"password" so current behavior is unchanged for every device that hasn't
opted in.
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing, drop_column_if_exists

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("devices", sa.Column("ssh_private_key_encrypted", sa.Text(), nullable=True))
    add_column_if_missing("devices", sa.Column("ssh_private_key_passphrase_encrypted", sa.Text(), nullable=True))
    add_column_if_missing(
        "devices",
        sa.Column("ssh_auth_method", sa.String(), nullable=False, server_default="password"),
    )


def downgrade() -> None:
    drop_column_if_exists("devices", "ssh_auth_method")
    drop_column_if_exists("devices", "ssh_private_key_passphrase_encrypted")
    drop_column_if_exists("devices", "ssh_private_key_encrypted")

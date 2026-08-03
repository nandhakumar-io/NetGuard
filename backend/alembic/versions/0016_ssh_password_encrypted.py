"""Direct SSH credential storage: ssh_password_encrypted column on devices

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-02

Context: app.models.device.Device.ssh_password_encrypted (a Fernet-encrypted
column letting an SSH password be stored directly on the device row,
taking priority over ssh_credential_ref env-var lookup -- see
app.services.credential_service.get_ssh_password / set_ssh_password, and
the "ssh_credentials_configured" flag in app.schemas.device) was added to
the model but never got a migration, so on any database that already ran
0001-0015 every query touching Device 500s with
`UndefinedColumn: devices.ssh_password_encrypted does not exist`
(GNS3 node lookups, the SNMP poll sweep, and anything else that loads a
Device row). This fills that gap.
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing, drop_column_if_exists

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("devices", sa.Column("ssh_password_encrypted", sa.Text(), nullable=True))


def downgrade() -> None:
    drop_column_if_exists("devices", "ssh_password_encrypted")
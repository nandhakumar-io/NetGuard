"""SNMPv3 support: add USM security columns + encrypted secret columns to devices

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-02

Adds the columns app.models.device.Device already defines for SNMPv3
(security level/auth protocol/priv protocol as enums, plus Fernet-encrypted
community/auth-key/priv-key columns) and snmp_port, none of which existed
in the schema yet -- app.services.snmp_service / metrics_service /
credential_service all assume these columns exist.
"""
import sqlalchemy as sa

from alembic import op
from migration_helpers import (
    add_column_if_missing,
    drop_column_if_exists,
    enum_type_exists,
)

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_SECURITY_LEVEL_ENUM = sa.Enum("noAuthNoPriv", "authNoPriv", "authPriv", name="snmpsecuritylevel")
_AUTH_PROTOCOL_ENUM = sa.Enum("MD5", "SHA", "SHA224", "SHA256", "SHA384", "SHA512", name="snmpauthprotocol")
_PRIV_PROTOCOL_ENUM = sa.Enum("DES", "3DES", "AES128", "AES192", "AES256", name="snmpprivprotocol")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "devices" not in inspector.get_table_names():
        # Fresh install: 0001's create_all brings the table in already
        # matching the current model, columns and all.
        return

    columns = {c["name"] for c in inspector.get_columns("devices")}

    if not enum_type_exists("snmpsecuritylevel"):
        _SECURITY_LEVEL_ENUM.create(bind, checkfirst=True)
    if not enum_type_exists("snmpauthprotocol"):
        _AUTH_PROTOCOL_ENUM.create(bind, checkfirst=True)
    if not enum_type_exists("snmpprivprotocol"):
        _PRIV_PROTOCOL_ENUM.create(bind, checkfirst=True)

    if "snmp_port" not in columns:
        add_column_if_missing("devices", sa.Column("snmp_port", sa.Integer(), nullable=True))
    if "snmp_security_level" not in columns:
        add_column_if_missing("devices", sa.Column("snmp_security_level", _SECURITY_LEVEL_ENUM, nullable=True))
    if "snmp_auth_protocol" not in columns:
        add_column_if_missing("devices", sa.Column("snmp_auth_protocol", _AUTH_PROTOCOL_ENUM, nullable=True))
    if "snmp_priv_protocol" not in columns:
        add_column_if_missing("devices", sa.Column("snmp_priv_protocol", _PRIV_PROTOCOL_ENUM, nullable=True))
    if "snmp_community_encrypted" not in columns:
        add_column_if_missing("devices", sa.Column("snmp_community_encrypted", sa.Text(), nullable=True))
    if "snmp_auth_key_encrypted" not in columns:
        add_column_if_missing("devices", sa.Column("snmp_auth_key_encrypted", sa.Text(), nullable=True))
    if "snmp_priv_key_encrypted" not in columns:
        add_column_if_missing("devices", sa.Column("snmp_priv_key_encrypted", sa.Text(), nullable=True))


def downgrade() -> None:
    drop_column_if_exists("devices", "snmp_priv_key_encrypted")
    drop_column_if_exists("devices", "snmp_auth_key_encrypted")
    drop_column_if_exists("devices", "snmp_community_encrypted")
    drop_column_if_exists("devices", "snmp_priv_protocol")
    drop_column_if_exists("devices", "snmp_auth_protocol")
    drop_column_if_exists("devices", "snmp_security_level")
    drop_column_if_exists("devices", "snmp_port")

    bind = op.get_bind()
    _PRIV_PROTOCOL_ENUM.drop(bind, checkfirst=True)
    _AUTH_PROTOCOL_ENUM.drop(bind, checkfirst=True)
    _SECURITY_LEVEL_ENUM.drop(bind, checkfirst=True)

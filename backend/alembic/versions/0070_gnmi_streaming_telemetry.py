"""gnmi_streaming_telemetry

Adds gNMI (dial-in SUBSCRIBE) streaming-telemetry support alongside the
existing SNMP polling path: per-device connection settings on `devices`
and a `source` column on `interface_metrics` so gNMI-pushed readings and
SNMP-polled readings coexist instead of one overwriting the other.

Revision ID: 0070
Revises: 0069
Create Date: 2026-08-18 00:00:00.000000
"""
import sqlalchemy as sa

from alembic import op
from migration_helpers import add_column_if_missing, drop_column_if_exists, table_exists

revision = '0070'
down_revision = '0069'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    dev_cols = {col['name'] for col in inspector.get_columns('devices')}
    if 'supports_gnmi' not in dev_cols:
        add_column_if_missing('devices', sa.Column('supports_gnmi', sa.Boolean(), nullable=False, server_default='false'))
    if 'gnmi_port' not in dev_cols:
        add_column_if_missing('devices', sa.Column('gnmi_port', sa.Integer(), nullable=True, server_default='9339'))
    if 'gnmi_use_tls' not in dev_cols:
        add_column_if_missing('devices', sa.Column('gnmi_use_tls', sa.Boolean(), nullable=False, server_default='true'))
    if 'gnmi_skip_verify' not in dev_cols:
        add_column_if_missing('devices', sa.Column('gnmi_skip_verify', sa.Boolean(), nullable=False, server_default='false'))
    if 'gnmi_username' not in dev_cols:
        add_column_if_missing('devices', sa.Column('gnmi_username', sa.String(), nullable=True))
    if 'gnmi_password_encrypted' not in dev_cols:
        add_column_if_missing('devices', sa.Column('gnmi_password_encrypted', sa.Text(), nullable=True))
    if 'gnmi_sample_interval_ms' not in dev_cols:
        add_column_if_missing('devices', sa.Column('gnmi_sample_interval_ms', sa.Integer(), nullable=True))
    if 'last_gnmi_update_at' not in dev_cols:
        add_column_if_missing('devices', sa.Column('last_gnmi_update_at', sa.DateTime(timezone=True), nullable=True))
    if 'last_gnmi_error' not in dev_cols:
        add_column_if_missing('devices', sa.Column('last_gnmi_error', sa.Text(), nullable=True))

    if table_exists('interface_metrics'):
        im_cols = {col['name'] for col in inspector.get_columns('interface_metrics')}
        if 'source' not in im_cols:
            add_column_if_missing('interface_metrics', sa.Column('source', sa.String(), nullable=False, server_default='snmp'))


def downgrade() -> None:
    drop_column_if_exists('interface_metrics', 'source')
    op.drop_column('devices', 'last_gnmi_error')
    op.drop_column('devices', 'last_gnmi_update_at')
    op.drop_column('devices', 'gnmi_sample_interval_ms')
    op.drop_column('devices', 'gnmi_password_encrypted')
    op.drop_column('devices', 'gnmi_username')
    op.drop_column('devices', 'gnmi_skip_verify')
    op.drop_column('devices', 'gnmi_use_tls')
    op.drop_column('devices', 'gnmi_port')
    op.drop_column('devices', 'supports_gnmi')

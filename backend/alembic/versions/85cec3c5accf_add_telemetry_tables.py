"""add_telemetry_tables

Revision ID: 85cec3c5accf
Revises: 0006
Create Date: 2026-07-31 16:05:01.009221

Idempotent rewrite: all table/column/enum creation is guarded so this
migration is safe to run against databases that were previously managed by
create_all() and may already have some or all of these objects.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import (
    add_column_if_missing,
    create_index_if_missing,
    create_table_if_missing,
    drop_column_if_exists,
    drop_index_if_exists,
)

revision = '85cec3c5accf'
down_revision = '0006'
branch_labels = None
depends_on = None


def _enum(*values, name):
    """Return a postgresql.ENUM with create_type=False so SA never fires the
    _on_table_create auto-DDL event.  Types are created explicitly below."""
    return postgresql.ENUM(*values, name=name, create_type=False)


def _create_enum_if_not_exists(bind, *values, name):
    """Create a PG enum type only if it doesn't already exist."""
    bind.execute(sa.text("SAVEPOINT _ceq"))
    try:
        vals = ", ".join(f"'{v}'" for v in values)
        bind.execute(sa.text(f"CREATE TYPE {name} AS ENUM ({vals})"))
        bind.execute(sa.text("RELEASE SAVEPOINT _ceq"))
    except Exception:
        bind.execute(sa.text("ROLLBACK TO SAVEPOINT _ceq"))
        bind.execute(sa.text("RELEASE SAVEPOINT _ceq"))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # ---------------------------------------------------------------
    # Ensure enum types exist (create_all may have already done this)
    # ---------------------------------------------------------------
    _create_enum_if_not_exists(bind, 'CRITICAL', 'WARNING', 'INFO', name='alertseverity')
    _create_enum_if_not_exists(bind, 'SNMP_TRAP', 'HEALTH_POLL', 'DRIFT', 'PROTOCOL_FAILURE', name='alertsource')
    _create_enum_if_not_exists(bind, 'GREEN', 'YELLOW', 'RED', name='healthcolor')
    _create_enum_if_not_exists(bind, 'NETCONF', 'RESTCONF', 'SNMP', name='protocolname')

    # ---------------------------------------------------------------
    # alerts
    # ---------------------------------------------------------------
    if 'alerts' not in existing_tables:
        create_table_if_missing(
            'alerts',
            sa.Column('id', sa.UUID(), nullable=False),
            sa.Column('device_id', sa.UUID(), nullable=True),
            sa.Column('severity', _enum('CRITICAL', 'WARNING', 'INFO', name='alertseverity'), nullable=False),
            sa.Column('source', _enum('SNMP_TRAP', 'HEALTH_POLL', 'DRIFT', 'PROTOCOL_FAILURE', name='alertsource'), nullable=False),
            sa.Column('category', sa.String(), nullable=False),
            sa.Column('message', sa.Text(), nullable=False),
            sa.Column('acknowledged', sa.Boolean(), server_default='false', nullable=False),
            sa.Column('acknowledged_by', sa.String(), nullable=True),
            sa.Column('resolved', sa.Boolean(), server_default='false', nullable=False),
            sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('resolved_by', sa.String(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
            sa.ForeignKeyConstraint(['device_id'], ['devices.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        create_index_if_missing(op.f('ix_alerts_created_at'), 'alerts', ['created_at'], unique=False)
        create_index_if_missing(op.f('ix_alerts_device_id'), 'alerts', ['device_id'], unique=False)

    # ---------------------------------------------------------------
    # device_metrics
    # ---------------------------------------------------------------
    if 'device_metrics' not in existing_tables:
        create_table_if_missing(
            'device_metrics',
            sa.Column('id', sa.UUID(), nullable=False),
            sa.Column('device_id', sa.UUID(), nullable=False),
            sa.Column('cpu_utilization_pct', sa.Float(), nullable=True),
            sa.Column('memory_utilization_pct', sa.Float(), nullable=True),
            sa.Column('interface_utilization_pct', sa.Float(), nullable=True),
            sa.Column('interface_errors', sa.Integer(), nullable=True),
            sa.Column('temperature_celsius', sa.Float(), nullable=True),
            sa.Column('fan_status', sa.String(), nullable=True),
            sa.Column('power_supply_status', sa.String(), nullable=True),
            sa.Column('uptime_seconds', sa.Integer(), nullable=True),
            sa.Column('interface_octets_total', sa.BigInteger(), nullable=True),
            sa.Column('interface_speed_bps', sa.BigInteger(), nullable=True),
            sa.Column('health_score', sa.Integer(), nullable=True),
            sa.Column('health_color', _enum('GREEN', 'YELLOW', 'RED', name='healthcolor'), nullable=True),
            sa.Column('polled_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
            sa.ForeignKeyConstraint(['device_id'], ['devices.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        create_index_if_missing(op.f('ix_device_metrics_device_id'), 'device_metrics', ['device_id'], unique=False)
        create_index_if_missing(op.f('ix_device_metrics_polled_at'), 'device_metrics', ['polled_at'], unique=False)

    # ---------------------------------------------------------------
    # protocol_operations
    # ---------------------------------------------------------------
    if 'protocol_operations' not in existing_tables:
        create_table_if_missing(
            'protocol_operations',
            sa.Column('id', sa.UUID(), nullable=False),
            sa.Column('device_id', sa.UUID(), nullable=True),
            sa.Column('protocol', _enum('NETCONF', 'RESTCONF', 'SNMP', name='protocolname'), nullable=False),
            sa.Column('operation', sa.String(), nullable=False),
            sa.Column('operator', sa.String(), nullable=False),
            sa.Column('request_payload', sa.Text(), nullable=True),
            sa.Column('response_payload', sa.Text(), nullable=True),
            sa.Column('http_status', sa.Integer(), nullable=True),
            sa.Column('success', sa.Boolean(), nullable=False),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.Column('execution_time_ms', sa.Float(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
            sa.ForeignKeyConstraint(['device_id'], ['devices.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        create_index_if_missing(op.f('ix_protocol_operations_created_at'), 'protocol_operations', ['created_at'], unique=False)
        create_index_if_missing(op.f('ix_protocol_operations_device_id'), 'protocol_operations', ['device_id'], unique=False)

    # ---------------------------------------------------------------
    # deployment_logs
    # ---------------------------------------------------------------
    if 'deployment_logs' not in existing_tables:
        create_table_if_missing(
            'deployment_logs',
            sa.Column('id', sa.UUID(), nullable=False),
            sa.Column('deployment_id', sa.UUID(), nullable=False),
            sa.Column('step', sa.String(), nullable=False),
            sa.Column('level', sa.String(), nullable=False),
            sa.Column('message', sa.Text(), nullable=False),
            sa.Column('timestamp', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
            sa.ForeignKeyConstraint(['deployment_id'], ['deployments.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        create_index_if_missing(op.f('ix_deployment_logs_deployment_id'), 'deployment_logs', ['deployment_id'], unique=False)
        create_index_if_missing(op.f('ix_deployment_logs_timestamp'), 'deployment_logs', ['timestamp'], unique=False)

    # ---------------------------------------------------------------
    # config_drifts: the 0002 migration now creates the final schema
    # directly, so these add/drop columns are only needed if 0002 created
    # the OLD schema.  We guard each operation individually.
    # ---------------------------------------------------------------
    cd_cols = {col['name'] for col in inspector.get_columns('config_drifts')}

    if 'baseline' not in cd_cols:
        _create_enum_if_not_exists(bind, 'GOLDEN_CONFIG', 'PREVIOUS_BACKUP', name='driftbaseline')
        add_column_if_missing('config_drifts', sa.Column(
            'baseline',
            postgresql.ENUM('GOLDEN_CONFIG', 'PREVIOUS_BACKUP', name='driftbaseline', create_type=False),
            nullable=False,
            server_default='PREVIOUS_BACKUP',
        ))
    if 'diff_text' not in cd_cols:
        add_column_if_missing('config_drifts', sa.Column('diff_text', sa.Text(), nullable=False, server_default=''))
    if 'added_lines' not in cd_cols:
        add_column_if_missing('config_drifts', sa.Column('added_lines', sa.Integer(), nullable=False, server_default='0'))
    if 'removed_lines' not in cd_cols:
        add_column_if_missing('config_drifts', sa.Column('removed_lines', sa.Integer(), nullable=False, server_default='0'))
    if 'modified_lines' not in cd_cols:
        add_column_if_missing('config_drifts', sa.Column('modified_lines', sa.Integer(), nullable=False, server_default='0'))
    if 'risk_score' not in cd_cols:
        add_column_if_missing('config_drifts', sa.Column('risk_score', sa.Integer(), nullable=False, server_default='0'))
    if 'compliance_score' not in cd_cols:
        add_column_if_missing('config_drifts', sa.Column('compliance_score', sa.Integer(), nullable=False, server_default='100'))
    if 'ai_summary' not in cd_cols:
        add_column_if_missing('config_drifts', sa.Column('ai_summary', sa.Text(), nullable=True))
    if 'status' not in cd_cols:
        _create_enum_if_not_exists(bind, 'OPEN', 'APPROVED', 'ROLLED_BACK', 'DISMISSED', name='driftstatus')
        add_column_if_missing('config_drifts', sa.Column(
            'status',
            postgresql.ENUM('OPEN', 'APPROVED', 'ROLLED_BACK', 'DISMISSED', name='driftstatus', create_type=False),
            nullable=False,
            server_default='OPEN',
        ))
    if 'detected_at' not in cd_cols:
        add_column_if_missing('config_drifts', sa.Column(
            'detected_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True
        ))

    # Create indexes on config_drifts if not already present
    existing_indexes = {idx['name'] for idx in inspector.get_indexes('config_drifts')}
    if 'ix_config_drifts_detected_at' not in existing_indexes:
        create_index_if_missing(op.f('ix_config_drifts_detected_at'), 'config_drifts', ['detected_at'], unique=False)
    if 'ix_config_drifts_device_id' not in existing_indexes:
        create_index_if_missing(op.f('ix_config_drifts_device_id'), 'config_drifts', ['device_id'], unique=False)

    # Drop old columns only if they still exist
    for old_col in ('baseline_snapshot_id', 'checked_at', 'detail', 'resolved_at',
                    'lines_changed', 'drifted', 'resolved_by', 'triggered_by', 'resolved', 'diff'):
        if old_col in cd_cols:
            if old_col == 'baseline_snapshot_id':
                try:
                    op.drop_constraint('config_drifts_baseline_snapshot_id_fkey', 'config_drifts', type_='foreignkey')
                except Exception:
                    pass
            drop_column_if_exists('config_drifts', old_col)

    # ---------------------------------------------------------------
    # devices: add new columns if missing
    # ---------------------------------------------------------------
    dev_cols = {col['name'] for col in inspector.get_columns('devices')}

    if 'platform' not in dev_cols:
        add_column_if_missing('devices', sa.Column('platform', sa.String(), nullable=True))
    if 'model' not in dev_cols:
        add_column_if_missing('devices', sa.Column('model', sa.String(), nullable=True))
    if 'serial_number' not in dev_cols:
        add_column_if_missing('devices', sa.Column('serial_number', sa.String(), nullable=True))
    if 'os_version' not in dev_cols:
        add_column_if_missing('devices', sa.Column('os_version', sa.String(), nullable=True))
    if 'supports_netconf' not in dev_cols:
        add_column_if_missing('devices', sa.Column('supports_netconf', sa.Boolean(), server_default='false', nullable=False))
    if 'supports_restconf' not in dev_cols:
        add_column_if_missing('devices', sa.Column('supports_restconf', sa.Boolean(), server_default='false', nullable=False))
    if 'supports_snmp' not in dev_cols:
        add_column_if_missing('devices', sa.Column('supports_snmp', sa.Boolean(), server_default='false', nullable=False))
    if 'netconf_port' not in dev_cols:
        add_column_if_missing('devices', sa.Column('netconf_port', sa.Integer(), nullable=True))
    if 'restconf_url' not in dev_cols:
        add_column_if_missing('devices', sa.Column('restconf_url', sa.String(), nullable=True))
    if 'snmp_version' not in dev_cols:
        _create_enum_if_not_exists(bind, 'V1', 'V2C', 'V3', name='snmpversion')
        add_column_if_missing('devices', sa.Column(
            'snmp_version',
            postgresql.ENUM('V1', 'V2C', 'V3', name='snmpversion', create_type=False),
            nullable=True,
        ))
    if 'snmp_community_ref' not in dev_cols:
        add_column_if_missing('devices', sa.Column('snmp_community_ref', sa.String(), nullable=True))
    if 'snmp_username' not in dev_cols:
        add_column_if_missing('devices', sa.Column('snmp_username', sa.String(), nullable=True))
    if 'snmp_auth_credential_ref' not in dev_cols:
        add_column_if_missing('devices', sa.Column('snmp_auth_credential_ref', sa.String(), nullable=True))
    if 'snmp_privacy_credential_ref' not in dev_cols:
        add_column_if_missing('devices', sa.Column('snmp_privacy_credential_ref', sa.String(), nullable=True))
    if 'capabilities' not in dev_cols:
        add_column_if_missing('devices', sa.Column('capabilities', sa.Text(), nullable=True))


def downgrade() -> None:
    drop_column_if_exists('devices', 'capabilities')
    drop_column_if_exists('devices', 'snmp_privacy_credential_ref')
    drop_column_if_exists('devices', 'snmp_auth_credential_ref')
    drop_column_if_exists('devices', 'snmp_username')
    drop_column_if_exists('devices', 'snmp_community_ref')
    drop_column_if_exists('devices', 'snmp_version')
    drop_column_if_exists('devices', 'restconf_url')
    drop_column_if_exists('devices', 'netconf_port')
    drop_column_if_exists('devices', 'supports_snmp')
    drop_column_if_exists('devices', 'supports_restconf')
    drop_column_if_exists('devices', 'supports_netconf')
    drop_column_if_exists('devices', 'os_version')
    drop_column_if_exists('devices', 'serial_number')
    drop_column_if_exists('devices', 'model')
    drop_column_if_exists('devices', 'platform')
    drop_index_if_exists(op.f('ix_deployment_logs_timestamp'), table_name='deployment_logs')
    drop_index_if_exists(op.f('ix_deployment_logs_deployment_id'), table_name='deployment_logs')
    op.drop_table('deployment_logs')
    drop_index_if_exists(op.f('ix_protocol_operations_device_id'), table_name='protocol_operations')
    drop_index_if_exists(op.f('ix_protocol_operations_created_at'), table_name='protocol_operations')
    op.drop_table('protocol_operations')
    drop_index_if_exists(op.f('ix_device_metrics_polled_at'), table_name='device_metrics')
    drop_index_if_exists(op.f('ix_device_metrics_device_id'), table_name='device_metrics')
    op.drop_table('device_metrics')
    drop_index_if_exists(op.f('ix_alerts_device_id'), table_name='alerts')
    drop_index_if_exists(op.f('ix_alerts_created_at'), table_name='alerts')
    op.drop_table('alerts')

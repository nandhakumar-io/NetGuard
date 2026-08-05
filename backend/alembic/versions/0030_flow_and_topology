"""add flow_records and topology_snapshots tables

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-05 00:00:00.000000

Idempotent (guarded by inspector checks), matching the pattern used by
0029/85cec3c5accf, so this is safe to run against a DB that already has
some of these objects.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0030'
down_revision = '0029'
branch_labels = None
depends_on = None


def _create_enum_if_not_exists(bind, *values, name):
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

    _create_enum_if_not_exists(bind, 'netflow_v5', 'netflow_v9', 'ipfix', 'sflow', name='flowprotocolversion')

    if 'flow_records' not in existing_tables:
        op.create_table(
            'flow_records',
            sa.Column('id', sa.UUID(), nullable=False),
            sa.Column('device_id', sa.UUID(), nullable=True),
            sa.Column('exporter_ip', sa.String(), nullable=False),
            sa.Column(
                'flow_version',
                postgresql.ENUM('netflow_v5', 'netflow_v9', 'ipfix', 'sflow', name='flowprotocolversion', create_type=False),
                nullable=False,
            ),
            sa.Column('sampling_rate', sa.Integer(), server_default='1', nullable=False),
            sa.Column('src_ip', sa.String(), nullable=False),
            sa.Column('dst_ip', sa.String(), nullable=False),
            sa.Column('src_port', sa.Integer(), nullable=True),
            sa.Column('dst_port', sa.Integer(), nullable=True),
            sa.Column('ip_protocol', sa.SmallInteger(), nullable=False),
            sa.Column('tos', sa.SmallInteger(), nullable=True),
            sa.Column('src_as', sa.Integer(), nullable=True),
            sa.Column('dst_as', sa.Integer(), nullable=True),
            sa.Column('input_snmp_if', sa.Integer(), nullable=True),
            sa.Column('output_snmp_if', sa.Integer(), nullable=True),
            sa.Column('tcp_flags', sa.SmallInteger(), nullable=True),
            sa.Column('bytes', sa.BigInteger(), nullable=False),
            sa.Column('packets', sa.BigInteger(), nullable=False),
            sa.Column('flow_start', sa.DateTime(timezone=True), nullable=True),
            sa.Column('flow_end', sa.DateTime(timezone=True), nullable=True),
            sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
            sa.ForeignKeyConstraint(['device_id'], ['devices.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(op.f('ix_flow_records_device_id'), 'flow_records', ['device_id'], unique=False)
        op.create_index(op.f('ix_flow_records_exporter_ip'), 'flow_records', ['exporter_ip'], unique=False)
        op.create_index(op.f('ix_flow_records_src_ip'), 'flow_records', ['src_ip'], unique=False)
        op.create_index(op.f('ix_flow_records_dst_ip'), 'flow_records', ['dst_ip'], unique=False)
        op.create_index(op.f('ix_flow_records_received_at'), 'flow_records', ['received_at'], unique=False)

    if 'topology_snapshots' not in existing_tables:
        op.create_table(
            'topology_snapshots',
            sa.Column('id', sa.UUID(), nullable=False),
            sa.Column('nodes_json', sa.Text(), nullable=False),
            sa.Column('edges_json', sa.Text(), nullable=False),
            sa.Column('node_count', sa.Integer(), nullable=False),
            sa.Column('edge_count', sa.Integer(), nullable=False),
            sa.Column('captured_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(
            op.f('ix_topology_snapshots_captured_at'), 'topology_snapshots', ['captured_at'], unique=False
        )


def downgrade() -> None:
    op.drop_index(op.f('ix_topology_snapshots_captured_at'), table_name='topology_snapshots')
    op.drop_table('topology_snapshots')
    op.drop_index(op.f('ix_flow_records_received_at'), table_name='flow_records')
    op.drop_index(op.f('ix_flow_records_dst_ip'), table_name='flow_records')
    op.drop_index(op.f('ix_flow_records_src_ip'), table_name='flow_records')
    op.drop_index(op.f('ix_flow_records_exporter_ip'), table_name='flow_records')
    op.drop_index(op.f('ix_flow_records_device_id'), table_name='flow_records')
    op.drop_table('flow_records')
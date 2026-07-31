"""add config_drifts table (Config Drift Detection)

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-31
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# Two distinct objects on purpose:
#   - `drift_severity_type`: used to explicitly CREATE TYPE ourselves,
#     guarded by checkfirst.
#   - `drift_severity_column`: used inside the Column/create_table call,
#     with create_type=False so SQLAlchemy doesn't *also* try to create
#     the type as a side effect of creating the table. Without this, even
#     a single, non-concurrent run of this migration fails: the explicit
#     .create(checkfirst=True) below succeeds, then op.create_table's own
#     "create any enum types used by its columns" step fires next and
#     tries to CREATE TYPE a second time -- that inner step does not
#     inherit our checkfirst, so it errors with "already exists" even
#     though nothing else touched the database in between.
drift_severity_type = sa.Enum("none", "low", "medium", "high", name="driftseverity")
drift_severity_column = sa.Enum("none", "low", "medium", "high", name="driftseverity", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "config_drifts" in inspector.get_table_names():
        return  # already created by baseline's create_all(checkfirst=True) on a fresh install

    drift_severity_type.create(bind, checkfirst=True)

    op.create_table(
        "config_drifts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("device_id", UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=False),
        sa.Column(
            "baseline_snapshot_id", UUID(as_uuid=True), sa.ForeignKey("config_snapshots.id"), nullable=True
        ),
        sa.Column("drifted", sa.String(), nullable=False, server_default="false"),
        sa.Column("severity", drift_severity_column, nullable=False, server_default="none"),
        sa.Column("lines_changed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("diff", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("triggered_by", sa.String(), nullable=False, server_default="scheduled"),
        sa.Column("resolved", sa.String(), nullable=False, server_default="false"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_config_drifts_device_id", "config_drifts", ["device_id"])


def downgrade() -> None:
    op.drop_index("ix_config_drifts_device_id", table_name="config_drifts")
    op.drop_table("config_drifts")
    drift_severity_type.drop(op.get_bind(), checkfirst=True)
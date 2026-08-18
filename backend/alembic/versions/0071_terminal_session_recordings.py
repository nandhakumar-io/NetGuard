"""terminal_session_recordings

Adds the terminal_session_recordings table backing full-transcript
session recording for privileged device terminal access (app.api.
terminal.device_terminal / app.services.session_recording_service) --
metadata + a jit_elevation_id link, with the actual transcript stored
on disk under settings.TERMINAL_RECORDING_DIR. See app.models.
terminal_session_recording for the full rationale (PCI/SOC2 privileged-
access session recording, not just start/stop audit log entries).

Revision ID: 0071
Revises: 0070
Create Date: 2026-08-18 00:00:00.000000
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from migration_helpers import create_index_if_missing, create_table_if_missing

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_table_if_missing(
        "terminal_session_recordings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("actor_email", sa.String(), nullable=False),
        sa.Column("jit_elevation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jit_elevations.id"), nullable=True),
        sa.Column("protocol", sa.String(), nullable=True),
        sa.Column("device_hostname", sa.String(), nullable=True),
        sa.Column("file_path", sa.String(), nullable=True),
        sa.Column("byte_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("redacted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(), nullable=True),
    )
    create_index_if_missing("ix_terminal_session_recordings_device_id", "terminal_session_recordings", ["device_id"])
    create_index_if_missing("ix_terminal_session_recordings_user_id", "terminal_session_recordings", ["user_id"])
    create_index_if_missing(
        "ix_terminal_session_recordings_jit_elevation_id", "terminal_session_recordings", ["jit_elevation_id"]
    )


def downgrade() -> None:
    from alembic import op

    op.drop_index("ix_terminal_session_recordings_jit_elevation_id", table_name="terminal_session_recordings")
    op.drop_index("ix_terminal_session_recordings_user_id", table_name="terminal_session_recordings")
    op.drop_index("ix_terminal_session_recordings_device_id", table_name="terminal_session_recordings")
    op.drop_table("terminal_session_recordings")

"""add syslog_messages, path_traces, path_hops

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-05

Two data-completeness gaps flagged against Auvik/SolarWinds NetPath:

  1. No syslog collection at all -- SNMP polling and interface counters
     only expose numeric/pollable state; auth failures, hardware fault
     events, and ACL deny hits are typically *only* ever emitted as
     syslog. syslog_messages is the raw capture; correlated_category/
     correlated_alert_id record the outcome of matching a message against
     app.services.syslog_service.CORRELATION_RULES.

  2. No hop-by-hop path/route tracing visualization -- path_traces/
     path_hops back a NetPath-style view: one row per trace run, with
     ordered hops (RTT/loss/status) either from a real traceroute or a
     topology-graph-derived fallback (see app.services.path_trace_service).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New AlertSource.SYSLOG member (app.models.alert) -- syslog-correlated
    # alerts (see app.services.syslog_service._correlate) need a source
    # value distinct from every existing SNMP-derived one. ADD VALUE can't
    # run inside the same transaction it's later read in on some PG
    # versions, but plain autocommit-per-statement (Alembic's default op.execute)
    # is sufficient here since nothing in this same migration reads the enum.
    bind = op.get_bind()
    exists = bind.execute(sa.text("SELECT 1 FROM pg_type WHERE typname = 'alertsource'")).scalar()
    if exists:
        op.execute("ALTER TYPE alertsource ADD VALUE IF NOT EXISTS 'syslog'")

    syslog_severity = postgresql.ENUM(
        "EMERGENCY", "ALERT", "CRITICAL", "ERROR", "WARNING", "NOTICE", "INFORMATIONAL", "DEBUG",
        name="syslogseverity",
    )
    syslog_severity.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "syslog_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=True),
        sa.Column("source_ip", sa.String(), nullable=False),
        sa.Column("facility", sa.Integer(), nullable=True),
        sa.Column("severity", syslog_severity, nullable=False, server_default="INFORMATIONAL"),
        sa.Column("reported_hostname", sa.String(), nullable=True),
        sa.Column("tag", sa.String(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("raw", sa.Text(), nullable=False),
        sa.Column("device_reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("correlated_category", sa.String(), nullable=True),
        sa.Column("correlated_alert_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("alerts.id"), nullable=True),
    )
    op.create_index("ix_syslog_messages_device_id", "syslog_messages", ["device_id"])
    op.create_index("ix_syslog_messages_source_ip", "syslog_messages", ["source_ip"])
    op.create_index("ix_syslog_messages_severity", "syslog_messages", ["severity"])
    op.create_index("ix_syslog_messages_received_at", "syslog_messages", ["received_at"])
    op.create_index("ix_syslog_messages_correlated_category", "syslog_messages", ["correlated_category"])

    path_trace_status = postgresql.ENUM("COMPLETE", "PARTIAL", "FAILED", name="pathtracestatus")
    path_trace_status.create(op.get_bind(), checkfirst=True)
    hop_status = postgresql.ENUM("OK", "DEGRADED", "TIMEOUT", "UNKNOWN", name="hopstatus")
    hop_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "path_traces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=True),
        sa.Column("source_ip", sa.String(), nullable=False),
        sa.Column("target_device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=True),
        sa.Column("target_input", sa.String(), nullable=False),
        sa.Column("target_resolved_ip", sa.String(), nullable=True),
        sa.Column("hop_source", sa.String(), nullable=False, server_default="topology"),
        sa.Column("status", path_trace_status, nullable=False, server_default="PARTIAL"),
        sa.Column("total_hops", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reached_target", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("requested_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_path_traces_source_device_id", "path_traces", ["source_device_id"])
    op.create_index("ix_path_traces_target_device_id", "path_traces", ["target_device_id"])
    op.create_index("ix_path_traces_created_at", "path_traces", ["created_at"])

    op.create_table(
        "path_hops",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("path_trace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("path_traces.id"), nullable=False),
        sa.Column("hop_index", sa.Integer(), nullable=False),
        sa.Column("ip_address", sa.String(), nullable=True),
        sa.Column("hostname", sa.String(), nullable=True),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=True),
        sa.Column("rtt_ms", sa.Float(), nullable=True),
        sa.Column("packet_loss_pct", sa.Float(), nullable=True),
        sa.Column("status", hop_status, nullable=False, server_default="UNKNOWN"),
    )
    op.create_index("ix_path_hops_path_trace_id", "path_hops", ["path_trace_id"])
    op.create_index("ix_path_hops_device_id", "path_hops", ["device_id"])


def downgrade() -> None:
    op.drop_table("path_hops")
    op.drop_table("path_traces")
    postgresql.ENUM(name="hopstatus").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="pathtracestatus").drop(op.get_bind(), checkfirst=True)

    op.drop_table("syslog_messages")
    postgresql.ENUM(name="syslogseverity").drop(op.get_bind(), checkfirst=True)
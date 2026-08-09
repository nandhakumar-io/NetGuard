"""incident / postmortem tracking

Revision ID: 0040
Revises: 0039
Create Date: 2026-08-09

Adds:
  - incidents table: a formal record built from a correlated alert group
    (see app.models.incident.Incident), with postmortem fields
    (root_cause_summary, impact_summary, action_items).
  - incident_timeline_events table: narrative timeline entries for an
    incident, independent of the raw Alert/AuditLog rows.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import create_index_if_missing, create_table_if_missing

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

_SEVERITY_ENUM = postgresql.ENUM("critical", "major", "minor", name="incidentseverity")
_STATUS_ENUM = postgresql.ENUM(
    "open", "mitigated", "resolved", "postmortem_due", "closed", name="incidentstatus"
)


def upgrade():
    bind = op.get_bind()
    _SEVERITY_ENUM.create(bind, checkfirst=True)
    _STATUS_ENUM.create(bind, checkfirst=True)

    create_table_if_missing(
        "incidents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("severity", _SEVERITY_ENUM, nullable=False, server_default="major"),
        sa.Column("status", _STATUS_ENUM, nullable=False, server_default="open"),
        sa.Column("root_cause_alert_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("alerts.id"), nullable=True),
        sa.Column("alert_ids", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mitigated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("root_cause_summary", sa.Text(), nullable=True),
        sa.Column("impact_summary", sa.Text(), nullable=True),
        sa.Column("action_items", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    create_index_if_missing("ix_incidents_root_cause_alert_id", "incidents", ["root_cause_alert_id"])
    create_index_if_missing("ix_incidents_created_at", "incidents", ["created_at"])

    create_table_if_missing(
        "incident_timeline_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False, server_default="note"),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    create_index_if_missing("ix_incident_timeline_events_incident_id", "incident_timeline_events", ["incident_id"])


def downgrade():
    op.drop_index("ix_incident_timeline_events_incident_id", table_name="incident_timeline_events")
    op.drop_table("incident_timeline_events")
    op.drop_index("ix_incidents_created_at", table_name="incidents")
    op.drop_index("ix_incidents_root_cause_alert_id", table_name="incidents")
    op.drop_table("incidents")
    bind = op.get_bind()
    _STATUS_ENUM.drop(bind, checkfirst=True)
    _SEVERITY_ENUM.drop(bind, checkfirst=True)

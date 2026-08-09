"""escalation policies + alerts escalation tracking columns

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-09

Adds:
  - escalation_policies table: user-defined "unacknowledged for N minutes
    -> notify secondary contact" rules (see
    app.models.escalation_policy.EscalationPolicy).
  - alerts.escalated / escalated_at / last_escalated_at /
    escalation_policy_id / escalation_count: tracks whether/when an
    alert has been escalated so the sweep (app.services.escalation_service)
    doesn't re-notify a one-shot policy repeatedly and can support
    repeat_minutes re-escalation for policies that want it.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import (
    add_column_if_missing,
    create_index_if_missing,
    create_table_if_missing,
)

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade():
    create_table_if_missing(
        "escalation_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "severity_scope",
            sa.Enum("critical", "warning", "all", name="escalationseverityscope"),
            nullable=False,
            server_default="critical",
        ),
        sa.Column("unack_minutes", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("repeat_minutes", sa.Integer(), nullable=True),
        sa.Column("secondary_contacts", sa.Text(), nullable=True),
        sa.Column(
            "channel",
            sa.Enum("email", "webhook", "slack", "teams", name="escalationchannel"),
            nullable=False,
            server_default="email",
        ),
        sa.Column("webhook_url", sa.String(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    add_column_if_missing("alerts", sa.Column("escalated", sa.Boolean(), nullable=False, server_default="false"))
    add_column_if_missing("alerts", sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True))
    add_column_if_missing("alerts", sa.Column("last_escalated_at", sa.DateTime(timezone=True), nullable=True))
    add_column_if_missing(
        "alerts",
        sa.Column("escalation_policy_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("escalation_policies.id"), nullable=True),
    )
    add_column_if_missing("alerts", sa.Column("escalation_count", sa.Integer(), nullable=False, server_default="0"))
    create_index_if_missing("ix_alerts_escalation_policy_id", "alerts", ["escalation_policy_id"])


def downgrade():
    op.drop_index("ix_alerts_escalation_policy_id", table_name="alerts")
    op.drop_column("alerts", "escalation_count")
    op.drop_column("alerts", "escalation_policy_id")
    op.drop_column("alerts", "last_escalated_at")
    op.drop_column("alerts", "escalated_at")
    op.drop_column("alerts", "escalated")
    op.drop_table("escalation_policies")
    sa.Enum(name="escalationchannel").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="escalationseverityscope").drop(op.get_bind(), checkfirst=True)

"""Add runbook remediation columns and runbook_executions table

Revision ID: 0093
Revises: 0092
Create Date: 2026-08-23

Turns AlertRunbooks from reference-only docs into something that can
optionally trigger a real remediation job (restart a service / push a
config snippet) against the alert's device -- see
app.services.runbook_execution_service and the new
POST /alert-runbooks/{id}/execute endpoint, which is gated by the same
require_roles(NETWORK_ADMIN) dependency used everywhere else a device
gets written to (that dependency already folds in JIT-elevation, see
app.core.deps.require_roles).

remediation_* columns are added nullable/default-off onto the existing
alert_runbooks table so every runbook created before this migration
keeps behaving exactly as a doc-only link.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from migration_helpers import (
    add_column_if_missing,
    create_index_if_missing,
    create_table_if_missing,
)

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy.dialects.postgresql import ENUM

    from alembic import op
    from migration_helpers import enum_type_exists

    if not enum_type_exists("remediationactiontype"):
        ENUM("restart_service", "push_config", name="remediationactiontype").create(op.get_bind())
    if not enum_type_exists("runbookexecutionstatus"):
        ENUM("pending", "success", "failed", name="runbookexecutionstatus").create(op.get_bind())

    add_column_if_missing(
        "alert_runbooks",
        sa.Column("remediation_enabled", sa.Boolean(), nullable=False, server_default="false"),
    )
    add_column_if_missing(
        "alert_runbooks",
        sa.Column(
            "remediation_action_type",
            ENUM("restart_service", "push_config", name="remediationactiontype", create_type=False),
            nullable=True,
        ),
    )
    add_column_if_missing("alert_runbooks", sa.Column("remediation_label", sa.String(), nullable=True))
    add_column_if_missing("alert_runbooks", sa.Column("remediation_command", sa.Text(), nullable=True))
    # userrole enum already exists (see app.models.user.UserRole / its
    # original migration) -- reuse it rather than redefining, so this
    # column stays in lockstep with any future role addition.
    add_column_if_missing(
        "alert_runbooks",
        sa.Column("remediation_required_role", ENUM(name="userrole", create_type=False), nullable=True),
    )

    create_table_if_missing(
        "runbook_executions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("runbook_id", UUID(as_uuid=True), nullable=False),
        sa.Column("alert_id", UUID(as_uuid=True), nullable=True),
        sa.Column("device_id", UUID(as_uuid=True), nullable=False),
        sa.Column("triggered_by", sa.String(), nullable=False),
        sa.Column(
            "status",
            ENUM("pending", "success", "failed", name="runbookexecutionstatus", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    create_index_if_missing("ix_runbook_executions_runbook_id", "runbook_executions", ["runbook_id"])
    create_index_if_missing("ix_runbook_executions_alert_id", "runbook_executions", ["alert_id"])
    create_index_if_missing("ix_runbook_executions_device_id", "runbook_executions", ["device_id"])


def downgrade() -> None:
    from alembic import op

    op.drop_table("runbook_executions")
    for col in (
        "remediation_required_role",
        "remediation_command",
        "remediation_label",
        "remediation_action_type",
        "remediation_enabled",
    ):
        op.drop_column("alert_runbooks", col)
    sa.Enum(name="runbookexecutionstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="remediationactiontype").drop(op.get_bind(), checkfirst=True)

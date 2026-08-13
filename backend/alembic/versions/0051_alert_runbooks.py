"""Add alert_runbooks table and runbook columns on alert_rules

Revision ID: 0051
Revises: 0050
Create Date: 2026-08-13

Runbook attachments on alerts -- link a remediation doc/playbook to an
alert type so a new on-call engineer isn't guessing. See
app.models.alert_runbook for the category(+source)->doc mapping used by
all alerts, and the new runbook_url/runbook_title columns on AlertRule
for a convenience default when a rule is created.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import (
    add_column_if_missing,
    create_table_if_missing,
    drop_column_if_exists,
)

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None

alert_source_enum = postgresql.ENUM(
    "snmp_trap", "health_poll", "drift", "protocol_failure", "syslog",
    name="alertsource", create_type=False,
)


def upgrade() -> None:
    add_column_if_missing("alert_rules", sa.Column("runbook_url", sa.String(), nullable=True))
    add_column_if_missing("alert_rules", sa.Column("runbook_title", sa.String(), nullable=True))

    create_table_if_missing(
        "alert_runbooks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("category", sa.String(), nullable=False, index=True),
        sa.Column("source", alert_source_enum, nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("category", "source", name="uq_alert_runbook_category_source"),
    )


def downgrade() -> None:
    op.drop_table("alert_runbooks")
    drop_column_if_exists("alert_rules", "runbook_title")
    drop_column_if_exists("alert_rules", "runbook_url")

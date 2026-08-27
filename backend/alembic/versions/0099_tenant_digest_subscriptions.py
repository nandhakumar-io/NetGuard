"""Add tenant_digest_subscriptions table

Revision ID: 0099
Revises: 0098

Backs the per-tenant Alert/Incident/AuditLog digest feature -- see
app.models.tenant_digest_subscription for the full rationale. One row
per tenant subscription (a tenant can have more than one -- e.g. a daily
critical-only digest to on-call plus a weekly full rollup to management)
rather than a single-row-per-tenant table, so cadence/recipients/severity
floor can vary independently per subscription.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from migration_helpers import create_index_if_missing, create_table_if_missing

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_table_if_missing(
        "tenant_digest_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("cadence", sa.Enum("daily", "weekly", name="digestcadence"), nullable=False, server_default="weekly"),
        sa.Column("hour_utc", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("day_of_week", sa.Integer(), nullable=True),
        sa.Column("recipients", sa.String(), nullable=False),
        sa.Column(
            "severity_floor",
            sa.Enum("all", "warning", "critical", name="digestseverityfloor"),
            nullable=False,
            server_default="all",
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    create_index_if_missing(
        "ix_tenant_digest_subscriptions_tenant_id", "tenant_digest_subscriptions", ["tenant_id"]
    )


def downgrade() -> None:
    from alembic import op

    op.drop_index("ix_tenant_digest_subscriptions_tenant_id", table_name="tenant_digest_subscriptions")
    op.drop_table("tenant_digest_subscriptions")
    sa.Enum(name="digestcadence").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="digestseverityfloor").drop(op.get_bind(), checkfirst=True)

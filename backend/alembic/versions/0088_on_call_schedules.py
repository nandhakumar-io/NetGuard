"""on-call schedules

Revision ID: 0088
Revises: 0087
Create Date: 2026-08-21

Adds on_call_schedules (see app.models.on_call_schedule.OnCallSchedule)
and escalation_policies.on_call_schedule_id -- the backend half of a
feature the Escalation Policies page has been calling since it was
built (GET /on-call-schedules, and a "New Policy" on-call-schedule
picker). Until this runs, that page's Promise.all([...]) 404s on the
missing route and the whole page fails to load, not just the on-call
picker -- see app.api.on_call_schedules.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from migration_helpers import (
    add_column_if_missing,
    create_foreign_key_if_missing,
    create_table_if_missing,
)

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op_get_bind()
    uuid_type = postgresql.UUID(as_uuid=True) if bind.dialect.name == "postgresql" else sa.String(36)

    create_table_if_missing(
        "on_call_schedules",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("primary_user_email", sa.String(), nullable=False),
        sa.Column("secondary_user_email", sa.String(), nullable=True),
        sa.Column(
            "rotation_type",
            sa.Enum("none", "daily", "weekly", name="oncallrotationtype"),
            nullable=False,
            server_default="none",
        ),
        sa.Column("shift_handover_time", sa.String(), nullable=True),
        sa.Column("timezone", sa.String(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    add_column_if_missing("escalation_policies", sa.Column("on_call_schedule_id", uuid_type, nullable=True))

    create_foreign_key_if_missing(
        "fk_escalation_policies_on_call_schedule_id",
        "escalation_policies",
        "on_call_schedules",
        ["on_call_schedule_id"],
        ["id"],
        ondelete="SET NULL",
    )


def op_get_bind():
    from alembic import op

    return op.get_bind()


def downgrade() -> None:
    from alembic import op

    op.drop_constraint("fk_escalation_policies_on_call_schedule_id", "escalation_policies", type_="foreignkey")
    op.drop_column("escalation_policies", "on_call_schedule_id")
    op.drop_table("on_call_schedules")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS oncallrotationtype")

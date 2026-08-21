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

from alembic import op
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
    bind = op.get_bind()
    uuid_type = postgresql.UUID(as_uuid=True) if bind.dialect.name == "postgresql" else sa.String(36)

    # Create the enum type idempotently BEFORE the table.
    # op.create_table with an inline sa.Enum emits CREATE TYPE unconditionally,
    # which raises DuplicateObject when 0001's create_all (or a prior partial
    # run of this migration) already created the type. The DO $$ block is the
    # same guard used in migrations 0040, 0041, and 0054.
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text(
            "DO $$ BEGIN "
            "  CREATE TYPE oncallrotationtype AS ENUM ('none', 'daily', 'weekly'); "
            "EXCEPTION WHEN duplicate_object THEN NULL; "
            "END $$"
        ))

    create_table_if_missing(
        "on_call_schedules",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("primary_user_email", sa.String(), nullable=False),
        sa.Column("secondary_user_email", sa.String(), nullable=True),
        sa.Column(
            "rotation_type",
            # create_type=False: enum already created by the DO block above;
            # prevents SQLAlchemy from emitting a second CREATE TYPE inside
            # op.create_table.
            sa.Enum("none", "daily", "weekly", name="oncallrotationtype", create_type=False),
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


def downgrade() -> None:
    op.drop_constraint("fk_escalation_policies_on_call_schedule_id", "escalation_policies", type_="foreignkey")
    op.drop_column("escalation_policies", "on_call_schedule_id")
    op.drop_table("on_call_schedules")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS oncallrotationtype")

"""escalation policy push channel

Revision ID: 0087
Revises: 0086
Create Date: 2026-08-20

Adds "push" as a selectable value on the escalationchannel Postgres enum
so an EscalationPolicy can target mobile/browser push directly, instead
of push notifications only ever firing unconditionally alongside
whichever channel (email/webhook/slack/teams) the policy was set to.
See app.models.escalation_policy.EscalationChannel and
app.services.escalation_service._send.
"""
from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # sqlite/tests store the enum as a plain string column with no
        # separate type to alter -- nothing to do.
        return
    # ALTER TYPE ... ADD VALUE can't run inside the transaction Alembic
    # normally wraps migrations in, so this needs autocommit.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE escalationchannel ADD VALUE IF NOT EXISTS 'push'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE -- removing an enum value
    # requires rebuilding the type, which isn't safe to do blindly if any
    # row is currently using 'push'. Left as a no-op; an operator who
    # needs this reverted should migrate affected rows off 'push' first.
    pass

"""add_syslog_destinations

Revision ID: 0083
Revises: 0082
Create Date: 2026-08-20 00:00:00.000000

Adds syslog_destinations: outbound remote-syslog forwarding targets
(NetGuard -> external log collector), the sender-side counterpart to the
existing inbound syslog listener/SyslogMessage table. See
app.models.syslog_destination / app.services.syslog_forward_service.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import create_table_if_missing

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def _enum(*values, name):
    return postgresql.ENUM(*values, name=name, create_type=False)


def _create_enum_if_not_exists(bind, *values, name):
    bind.execute(sa.text("SAVEPOINT _sdeq"))
    try:
        vals = ", ".join(f"'{v}'" for v in values)
        bind.execute(sa.text(f"CREATE TYPE {name} AS ENUM ({vals})"))
        bind.execute(sa.text("RELEASE SAVEPOINT _sdeq"))
    except Exception:
        bind.execute(sa.text("ROLLBACK TO SAVEPOINT _sdeq"))


def upgrade() -> None:
    bind = op.get_bind()
    _create_enum_if_not_exists(bind, "udp", "tcp", name="syslogprotocol")

    create_table_if_missing(
        "syslog_destinations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="514"),
        sa.Column("protocol", _enum("udp", "tcp", name="syslogprotocol"), nullable=False, server_default="udp"),
        sa.Column("facility", sa.Integer(), nullable=False, server_default="16"),
        sa.Column("min_severity", sa.String(), nullable=False, server_default="info"),
        sa.Column("use_rfc5424", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("syslog_destinations")
    op.execute("DROP TYPE IF EXISTS syslogprotocol")

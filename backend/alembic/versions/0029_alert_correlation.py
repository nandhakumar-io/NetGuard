"""add topology-aware alert correlation columns

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-05

Third data-completeness gap flagged against Auvik/SolarWinds: no
topology-aware alert correlation. When a core device drops, every device
only reachable through it fires its own independent alert a poll cycle or
two later -- a storm of alerts instead of one root cause with the rest
flagged as impacted.

root_cause_alert_id / suppressed let app.services.alert_correlation_service
mark an alert as a likely *consequence* of another (already-active) alert,
derived by walking app.services.topology_service's graph from the failed
device. Suppressed alerts are still stored and independently resolvable;
they're just flagged so the UI can collapse them under their root cause.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("alerts", sa.Column("root_cause_alert_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("alerts", sa.Column("suppressed", sa.Boolean(), nullable=False, server_default="false"))
    op.create_index("ix_alerts_root_cause_alert_id", "alerts", ["root_cause_alert_id"])
    op.create_foreign_key(
        "fk_alerts_root_cause_alert_id", "alerts", "alerts", ["root_cause_alert_id"], ["id"]
    )


def downgrade():
    op.drop_constraint("fk_alerts_root_cause_alert_id", "alerts", type_="foreignkey")
    op.drop_index("ix_alerts_root_cause_alert_id", table_name="alerts")
    op.drop_column("alerts", "suppressed")
    op.drop_column("alerts", "root_cause_alert_id")
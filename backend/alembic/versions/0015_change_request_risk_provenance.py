"""Change request risk provenance: config_source, risk_engine_backend, risk_llm_applied/error

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-02

Adds the columns app.models.change_request.ChangeRequest already defines
so a reviewer can tell whether a CR's risk score came from the rule
engine alone or actually got an LLM pass (the "AI-reviewed" badge), and
whether current_config was a fresh live read or a stale snapshot
fallback -- both needed by the new POST /change-requests/{id}/rescore
retry action.
"""
import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "change_requests" not in inspector.get_table_names():
        # Fresh install: 0001's create_all brings the table in already
        # matching the current model, columns and all.
        return

    columns = {c["name"] for c in inspector.get_columns("change_requests")}

    if "config_source" not in columns:
        op.add_column("change_requests", sa.Column("config_source", sa.String(), nullable=True))
    if "risk_engine_backend" not in columns:
        op.add_column("change_requests", sa.Column("risk_engine_backend", sa.String(), nullable=True))
    if "risk_llm_applied" not in columns:
        op.add_column(
            "change_requests",
            sa.Column("risk_llm_applied", sa.Boolean(), nullable=False, server_default="false"),
        )
    if "risk_llm_error" not in columns:
        op.add_column("change_requests", sa.Column("risk_llm_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("change_requests", "risk_llm_error")
    op.drop_column("change_requests", "risk_llm_applied")
    op.drop_column("change_requests", "risk_engine_backend")
    op.drop_column("change_requests", "config_source")

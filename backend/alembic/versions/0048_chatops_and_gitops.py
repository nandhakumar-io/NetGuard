"""ChatOps identity links + GitOps repo config table

Revision ID: 0048
Revises: 0047
Create Date: 2026-08-11

Adds:
  - users.slack_user_id / users.msteams_user_id: nullable, unique links
    from a Slack/Teams account to a NetGuard user, set only via
    POST /chatops/links (Network Admin only). Backs two-way ChatOps
    (approve/reject/rollback/status commands from Slack or Teams).
  - git_repo_configs: one row per Git repository wired up for
    config-as-code sync of ConfigTemplate bodies (see
    app.models.git_repo_config.GitRepoConfig / app.services.git_sync_service).
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import add_column_if_missing, create_table_if_missing

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("users", sa.Column("slack_user_id", sa.String(), nullable=True))
    add_column_if_missing("users", sa.Column("msteams_user_id", sa.String(), nullable=True))
    if not any(
        ix["name"] == "ix_users_slack_user_id"
        for ix in sa.inspect(op.get_bind()).get_indexes("users")
    ):
        op.create_index("ix_users_slack_user_id", "users", ["slack_user_id"], unique=True)
    if not any(
        ix["name"] == "ix_users_msteams_user_id"
        for ix in sa.inspect(op.get_bind()).get_indexes("users")
    ):
        op.create_index("ix_users_msteams_user_id", "users", ["msteams_user_id"], unique=True)

    create_table_if_missing(
        "git_repo_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("repo_url", sa.String(), nullable=False),
        sa.Column("branch", sa.String(), nullable=False, server_default="main"),
        sa.Column("template_path", sa.String(), nullable=False, server_default="templates/"),
        sa.Column(
            "direction",
            sa.Enum("pull", "push", "bidirectional", name="gitsyncdirection"),
            nullable=False,
            server_default="pull",
        ),
        sa.Column("auto_sync_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("webhook_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("last_synced_commit", sa.String(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_sync_status",
            sa.Enum("never_synced", "syncing", "succeeded", "failed", name="gitsyncstatus"),
            nullable=False,
            server_default="never_synced",
        ),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("git_repo_configs")
    sa.Enum(name="gitsyncdirection").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="gitsyncstatus").drop(op.get_bind(), checkfirst=True)
    op.drop_index("ix_users_msteams_user_id", table_name="users")
    op.drop_index("ix_users_slack_user_id", table_name="users")
    op.drop_column("users", "msteams_user_id")
    op.drop_column("users", "slack_user_id")

"""Add tenants table, tenant_id on devices/users, is_msp_staff on users

Revision ID: 0092
Revises: 0091

Retrofits multi-tenancy onto what had been a genuinely single-tenant app
(see the comment in app.api.devices.get_placement_tree, which literally
said "there's no company node in the UI, since this app is
single-tenant"). This is what app.api.tenant_board (the cross-tenant NOC
board for MSP staff watching many customers at once) is built on top of.

Backward-compatible by construction: a single "Default" Tenant row is
created and every pre-existing Device/User is backfilled onto it, so
nothing that queried devices/users before this migration breaks. New
installs get the same Default tenant from seed_admin.py / seed_demo.py
going forward; MSP staff accounts (is_msp_staff=true) are the only rows
expected to stay tenant_id=NULL.
"""
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from migration_helpers import (
    add_column_if_missing,
    create_table_if_missing,
)

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def upgrade() -> None:
    create_table_if_missing(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    bind = op.get_bind()
    # Seed the Default tenant every pre-existing row backfills onto, if
    # it isn't already there (idempotent for re-runs / fresh installs
    # that create it via seed_admin.py instead).
    bind.execute(
        sa.text(
            "INSERT INTO tenants (id, name, slug, is_active) "
            "VALUES (:id, 'Default', 'default', true) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": str(_DEFAULT_TENANT_ID)},
    )

    add_column_if_missing(
        "devices", sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    add_column_if_missing(
        "users", sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    add_column_if_missing(
        "users",
        sa.Column("is_msp_staff", sa.Boolean(), nullable=False, server_default="false"),
    )

    # Backfill every existing device/user onto Default so nothing tenant-
    # scoped comes up empty for accounts that existed before this
    # migration. New rows created after this point are expected to set
    # tenant_id explicitly (see app.api.devices / app.api.user_management).
    bind.execute(
        sa.text("UPDATE devices SET tenant_id = :id WHERE tenant_id IS NULL"),
        {"id": str(_DEFAULT_TENANT_ID)},
    )
    bind.execute(
        sa.text(
            "UPDATE users SET tenant_id = :id WHERE tenant_id IS NULL AND is_msp_staff = false"
        ),
        {"id": str(_DEFAULT_TENANT_ID)},
    )

    with op.get_context().autocommit_block():
        op.execute("CREATE INDEX IF NOT EXISTS ix_devices_tenant_id ON devices (tenant_id)")
        op.execute("CREATE INDEX IF NOT EXISTS ix_users_tenant_id ON users (tenant_id)")

    op.create_foreign_key(
        "fk_devices_tenant_id", "devices", "tenants", ["tenant_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_users_tenant_id", "users", "tenants", ["tenant_id"], ["id"]
    )

    # AlertSource gains "anomaly" for app.services.anomaly_service --
    # same ADD VALUE / autocommit pattern as 0089.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE alertsource ADD VALUE IF NOT EXISTS 'anomaly'")


def downgrade() -> None:
    op.drop_constraint("fk_users_tenant_id", "users", type_="foreignkey")
    op.drop_constraint("fk_devices_tenant_id", "devices", type_="foreignkey")
    op.drop_index("ix_users_tenant_id", table_name="users")
    op.drop_index("ix_devices_tenant_id", table_name="devices")
    op.drop_column("users", "is_msp_staff")
    op.drop_column("users", "tenant_id")
    op.drop_column("devices", "tenant_id")
    op.drop_table("tenants")
    # No DROP VALUE for the alertsource enum -- same rationale as 0089/0062.

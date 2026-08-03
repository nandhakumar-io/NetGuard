"""Deployment pipeline circuit breaker: flagged_unstable / unstable_since
columns on devices

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-02

Fills a gap in the revision chain: this migration was originally numbered
0013, but a second, unrelated 0013 (SNMPv3 device columns) was added
later and collided with it -- 0015's down_revision was already written
as "0014" expecting this to exist under that number, so it's filled here
rather than renumbered, to avoid a second edit to 0015.

A device that fails deployment (FAILED or ROLLED_BACK) settings.
DEPLOYMENT_CIRCUIT_BREAKER_FAILURE_THRESHOLD times in a row, counted per
distinct ChangeRequest, is flagged unstable and blocked from further
automated deploys until a Network Administrator clears the flag via
POST /devices/{id}/clear-unstable-flag (see
app.services.pipeline_service._check_circuit_breaker).
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "devices", sa.Column("flagged_unstable", sa.Boolean(), nullable=False, server_default="false")
    )
    add_column_if_missing("devices", sa.Column("unstable_since", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    from alembic import op

    op.drop_column("devices", "unstable_since")
    op.drop_column("devices", "flagged_unstable")
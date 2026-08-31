"""Unattributed drift flag (Section 15 support)

Revision ID: 0120
Revises: 0119
Create Date: 2026-08-30

Section 15 (Break-Glass Access) requires an emergency management path
that is INDEPENDENT of NetGuard -- by design, that means NetGuard
cannot see or gate it directly (an emergency path that depends on
NetGuard being up isn't independent). What NetGuard *can* do, without
compromising that independence, is notice the aftermath: a config
change that shows up in drift detection with no corresponding approved/
deployed ChangeRequest around the time it happened is exactly what
break-glass (or, just as plausibly, an unauthorized out-of-band change)
looks like from NetGuard's side. That distinction previously didn't
exist -- every open drift was treated the same regardless of whether it
was expected.

Adds `unattributed` (bool) to config_drifts, set by
drift_service.detect_drift when no ChangeRequest for the same device
reached DEPLOYED status in the lookback window and the device isn't in
an active maintenance window either. Existing rows default to false
(unknown, not asserted as attributed) rather than back-computing this
for history that pre-dates the check.
"""
import sqlalchemy as sa

from alembic import op

revision = "0120"
down_revision = "0119"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "config_drifts",
        sa.Column("unattributed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_config_drifts_unattributed", "config_drifts", ["unattributed"])


def downgrade() -> None:
    op.drop_index("ix_config_drifts_unattributed", table_name="config_drifts")
    op.drop_column("config_drifts", "unattributed")

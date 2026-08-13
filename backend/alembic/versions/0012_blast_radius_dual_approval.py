"""Blast-radius dual approval: dual_approval_reason column on
change_requests (SRS 6.2 / FR-6 extension)

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-01

requires_dual_approval can now be triggered by either Critical Risk
classification or by additional_device_ids fanning a change out past
settings.RISK_BLAST_RADIUS_DUAL_APPROVAL_THRESHOLD devices, regardless of
risk score (see app.api.change_requests.create_change_request).
dual_approval_reason records which reason(s) applied, purely for the
audit trail / UI -- the approve() gate itself still only checks the
existing requires_dual_approval boolean.
"""
import sqlalchemy as sa

from migration_helpers import add_column_if_missing, drop_column_if_exists

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("change_requests", sa.Column("dual_approval_reason", sa.String(), nullable=True))


def downgrade() -> None:
    drop_column_if_exists("change_requests", "dual_approval_reason")

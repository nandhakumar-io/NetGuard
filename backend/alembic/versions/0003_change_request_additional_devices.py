"""add change_requests.additional_device_ids (SRS 6.6 multi-device deployment)

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-31

Context: pipeline_service.target_device_ids() and the change-requests API
have referenced ChangeRequest.additional_device_ids since multi-device
deployment support was added, but the column was never actually created
on the model or in a migration -- approving any change request raised
`AttributeError: 'ChangeRequest' object has no attribute
'additional_device_ids'`. This migration adds it as a nullable JSON-encoded
text column (a list of device UUID strings), matching how it's written in
api/change_requests.create_change_request and read in
pipeline_service.target_device_ids.
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns("change_requests")}
    if "additional_device_ids" in existing_columns:
        return  # already present (e.g. fresh install via create_all)

    op.add_column("change_requests", sa.Column("additional_device_ids", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("change_requests", "additional_device_ids")
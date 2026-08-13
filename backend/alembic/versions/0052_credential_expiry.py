"""Add devices.credentials_rotated_at for credential expiry countdown

Revision ID: 0052
Revises: 0051
Create Date: 2026-08-13

Backs the credential-expiry countdown badge -- stamped whenever SSH/SNMP
credentials are set via POST /devices/{id}/ssh-credentials,
/snmp-credentials, or the bulk ROTATE_CREDENTIALS action. See
app.services.credential_service.credential_expiry.
"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "devices", sa.Column("credentials_rotated_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("devices", "credentials_rotated_at")

"""Add blockchain_evidence table (Hyperledger Fabric evidence layer)

Revision ID: 0124
Revises: 0123
Create Date: 2026-09-02

Off-chain half of the "off-chain evidence + on-chain hash" split -- see
backend/app/services/evidence_service.py and fabric_service.py. This
table never stores secrets (SSH keys, SNMP communities, OpenBao/Keycloak
tokens); it stores the same kind of structured decision metadata already
present on ChangeRequest.policy_validation_result/batfish_result, plus
the SHA-256 hash of that metadata and the Fabric transaction that anchors
it.

New table only -- no existing table is touched.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None

EVIDENCE_TYPES = (
    "CHANGE_REQUEST_CREATED", "CHANGE_VALIDATION", "OPA_DECISION", "BATFISH_VALIDATION",
    "CHANGE_APPROVED", "CHANGE_REJECTED", "DEPLOYMENT_STARTED", "DEPLOYMENT_COMPLETED",
    "DEPLOYMENT_FAILED", "POST_DEPLOYMENT_VERIFICATION", "ROLLBACK_STARTED",
    "ROLLBACK_COMPLETED", "ROLLBACK_FAILED", "CONFIGURATION_BASELINE",
    "CONFIGURATION_DRIFT", "POLICY_VERSION",
)
ANCHOR_STATUSES = ("PENDING", "ANCHORING", "ANCHORED", "FAILED", "VERIFIED", "MISMATCH")


def upgrade() -> None:
    evidence_type_enum = postgresql.ENUM(*EVIDENCE_TYPES, name="evidencetype")
    anchor_status_enum = postgresql.ENUM(*ANCHOR_STATUSES, name="anchorstatus")
    bind = op.get_bind()
    evidence_type_enum.create(bind, checkfirst=True)
    anchor_status_enum.create(bind, checkfirst=True)

    op.create_table(
        "blockchain_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("evidence_id", sa.String(length=64), nullable=False),
        sa.Column("evidence_type", evidence_type_enum, nullable=False),
        sa.Column("change_request_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("change_requests.id"), nullable=True),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=True),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("config_snapshots.id"), nullable=True),
        sa.Column("deployment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("deployments.id"), nullable=True),
        sa.Column("evidence_body", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_hash", sa.String(length=71), nullable=False),
        sa.Column("configuration_hash", sa.String(length=71), nullable=True),
        sa.Column("hash_algorithm", sa.String(length=16), nullable=False, server_default="SHA-256"),
        sa.Column("canonicalization_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("previous_evidence_id", sa.String(length=64), nullable=True),
        sa.Column("previous_evidence_hash", sa.String(length=71), nullable=True),
        sa.Column("fabric_channel", sa.String(length=128), nullable=True),
        sa.Column("fabric_chaincode", sa.String(length=128), nullable=True),
        sa.Column("fabric_transaction_id", sa.String(length=128), nullable=True),
        sa.Column("fabric_block_number", sa.Integer(), nullable=True),
        sa.Column("anchor_status", anchor_status_enum, nullable=False, server_default="PENDING"),
        sa.Column("anchor_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("anchor_error", sa.Text(), nullable=True),
        sa.Column("policy_version", sa.String(length=64), nullable=True),
        sa.Column("batfish_version", sa.String(length=64), nullable=True),
        sa.Column("validation_engine_version", sa.String(length=64), nullable=True),
        sa.Column("application_version", sa.String(length=64), nullable=True),
        sa.Column("actor_subject", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("anchored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True),
    )
    op.create_unique_constraint("uq_blockchain_evidence_evidence_id", "blockchain_evidence", ["evidence_id"])
    op.create_index("ix_blockchain_evidence_evidence_id", "blockchain_evidence", ["evidence_id"])
    op.create_index("ix_blockchain_evidence_evidence_type", "blockchain_evidence", ["evidence_type"])
    op.create_index("ix_blockchain_evidence_change_request_id", "blockchain_evidence", ["change_request_id"])
    op.create_index("ix_blockchain_evidence_device_id", "blockchain_evidence", ["device_id"])
    op.create_index("ix_blockchain_evidence_fabric_transaction_id", "blockchain_evidence", ["fabric_transaction_id"])
    op.create_index("ix_blockchain_evidence_previous_evidence_id", "blockchain_evidence", ["previous_evidence_id"])
    op.create_index("ix_blockchain_evidence_tenant_id", "blockchain_evidence", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_blockchain_evidence_tenant_id", table_name="blockchain_evidence")
    op.drop_index("ix_blockchain_evidence_previous_evidence_id", table_name="blockchain_evidence")
    op.drop_index("ix_blockchain_evidence_fabric_transaction_id", table_name="blockchain_evidence")
    op.drop_index("ix_blockchain_evidence_device_id", table_name="blockchain_evidence")
    op.drop_index("ix_blockchain_evidence_change_request_id", table_name="blockchain_evidence")
    op.drop_index("ix_blockchain_evidence_evidence_type", table_name="blockchain_evidence")
    op.drop_index("ix_blockchain_evidence_evidence_id", table_name="blockchain_evidence")
    op.drop_constraint("uq_blockchain_evidence_evidence_id", "blockchain_evidence", type_="unique")
    op.drop_table("blockchain_evidence")
    postgresql.ENUM(*ANCHOR_STATUSES, name="anchorstatus").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(*EVIDENCE_TYPES, name="evidencetype").drop(op.get_bind(), checkfirst=True)

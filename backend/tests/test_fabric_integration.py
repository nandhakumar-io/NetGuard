"""Fabric service integration tests.

Tests the full evidence lifecycle (anchor → verify → tamper detection) and
the configuration-integrity check using an in-memory SQLite DB and a mocked
fabric_gateway_client so no live Fabric network is required.

Key design facts learned from reading fabric_service.py:
- anchor_evidence() ALWAYS creates the Postgres row regardless of FABRIC_ENABLED.
  When FABRIC_ENABLED=False it returns after creating the row (PENDING).
- submit_pending() is imported lazily: `from app.services import fabric_gateway_client`.
  The correct patch target is `app.services.fabric_gateway_client.submit_evidence`.
- check_configuration_integrity(None, config) intentionally returns (False, hash) --
  "caller bug, not a tamper signal". Callers must not pass None approved_hash
  and expect a pass.
- record.fabric_transaction_id  (not fabric_tx_id)
- verify_evidence() falls back to stored evidence_hash when FABRIC_ENABLED=False.

Coverage
--------
- anchor_evidence creates a row and (sync) anchors it
- submit_pending transitions PENDING → ANCHORED
- idempotent submit: calling submit_pending again on an ANCHORED row is a no-op
- verify_evidence: match returns verified=True
- tamper detection: modifying evidence_body causes verify_evidence to return
  verified=False with mismatched hashes
- configuration_integrity check: pass and fail scenarios
- FABRIC_UNAVAILABLE (FabricGatewayError) leaves the row PENDING
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Must set env before importing settings-dependent modules.
os.environ.setdefault("NETGUARD_CRED_TEST_ORCH_DEVICE", "test-password")

from app.core.database import Base
from app.models.blockchain_evidence import (
    AnchorStatus,
    BlockchainEvidence,
    EvidenceType,
)
from app.models.change_request import ChangeRequest, ChangeStatus
from app.models.device import Device, DeviceVendor
from app.services import fabric_service
from app.services.evidence_service import hash_config
from app.services.fabric_gateway_client import FabricGatewayError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture()
def device(db):
    d = Device(
        hostname="test-rtr",
        ip_address="10.0.0.1",
        vendor=DeviceVendor.CISCO,
        ssh_username="admin",
        ssh_credential_ref="test-rtr",
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


@pytest.fixture()
def cr(db, device):
    c = ChangeRequest(
        device_id=device.id,
        submitted_by=uuid.uuid4(),
        priority="medium",
        description="test change",
        proposed_config="hostname router1\n!",
        status=ChangeStatus.PENDING_APPROVAL,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _mock_submit_response(evidence_id: str, evidence_hash: str = "") -> dict:
    return {
        "transaction_id": f"tx-{evidence_id[:8]}",
        "block_number": 42,
        "evidence_id": evidence_id,
        "evidence_hash": evidence_hash,
    }


# ---------------------------------------------------------------------------
# anchor_evidence -- synchronous path
# ---------------------------------------------------------------------------


def test_anchor_evidence_sync_creates_anchored_row(db, device, cr):
    """When FABRIC_ASYNC_ANCHOR=False, anchor_evidence creates a PENDING row
    and immediately submits it (synchronously) via submit_pending."""
    with patch("app.core.config.settings.FABRIC_ENABLED", True), \
         patch("app.core.config.settings.FABRIC_ASYNC_ANCHOR", False), \
         patch(
             "app.services.fabric_gateway_client.submit_evidence",
             side_effect=lambda payload: _mock_submit_response(
                 payload["evidence_id"], payload.get("evidence_hash", "")
             ),
         ):
        record = fabric_service.anchor_evidence(
            db,
            evidence_type=EvidenceType.CHANGE_VALIDATION,
            change_request=cr,
            device=device,
            actor_subject="admin@test.com",
            fields={"result": "pass"},
        )

    db.refresh(record)
    assert record.anchor_status == AnchorStatus.ANCHORED
    assert record.fabric_transaction_id is not None
    assert record.fabric_transaction_id.startswith("tx-")
    assert record.fabric_block_number == 42
    assert record.evidence_hash is not None
    assert record.evidence_hash.startswith("sha256:")


def test_anchor_evidence_fabric_disabled_row_created_pending(db, device, cr):
    """When FABRIC_ENABLED=False, anchor_evidence still creates the Postgres
    row (for dev audit trail) but leaves it PENDING permanently."""
    with patch("app.core.config.settings.FABRIC_ENABLED", False):
        record = fabric_service.anchor_evidence(
            db,
            evidence_type=EvidenceType.CHANGE_VALIDATION,
            change_request=cr,
            device=device,
            actor_subject="admin@test.com",
            fields={},
        )
    db.refresh(record)
    assert record is not None
    assert record.anchor_status == AnchorStatus.PENDING
    assert db.query(BlockchainEvidence).count() == 1


def test_anchor_evidence_async_enqueues_not_anchors(db, device, cr):
    """With FABRIC_ASYNC_ANCHOR=True (default), anchor_evidence creates a
    PENDING row and dispatches a Celery task instead of submitting synchronously."""
    dispatched = []

    def _fake_dispatch(evidence_id):
        dispatched.append(evidence_id)

    with patch("app.core.config.settings.FABRIC_ENABLED", True), \
         patch("app.core.config.settings.FABRIC_ASYNC_ANCHOR", True), \
         patch("app.services.fabric_service._dispatch_anchor_task", side_effect=_fake_dispatch):
        record = fabric_service.anchor_evidence(
            db,
            evidence_type=EvidenceType.CHANGE_VALIDATION,
            change_request=cr,
            device=device,
            actor_subject="admin@test.com",
            fields={"result": "pass"},
        )

    db.refresh(record)
    assert record.anchor_status == AnchorStatus.PENDING
    assert dispatched == [record.evidence_id]


# ---------------------------------------------------------------------------
# submit_pending
# ---------------------------------------------------------------------------


def test_idempotent_submit_already_anchored(db, device, cr):
    """Calling submit_pending on an already-ANCHORED row must be a no-op and
    not make a second call to the gateway."""
    with patch("app.core.config.settings.FABRIC_ENABLED", True), \
         patch("app.core.config.settings.FABRIC_ASYNC_ANCHOR", False), \
         patch(
             "app.services.fabric_gateway_client.submit_evidence",
             side_effect=lambda payload: _mock_submit_response(
                 payload["evidence_id"], payload.get("evidence_hash", "")
             ),
         ) as mock_sub:
        record = fabric_service.anchor_evidence(
            db,
            evidence_type=EvidenceType.CHANGE_VALIDATION,
            change_request=cr,
            device=device,
            actor_subject="admin@test.com",
            fields={},
        )
        db.refresh(record)
        assert record.anchor_status == AnchorStatus.ANCHORED
        call_count_after_first = mock_sub.call_count

        # Call submit_pending again -- should be a no-op.
        with patch(
            "app.services.fabric_gateway_client.submit_evidence",
            side_effect=lambda payload: _mock_submit_response(payload["evidence_id"]),
        ) as mock_sub2:
            fabric_service.submit_pending(db, record)
            assert mock_sub2.call_count == 0, "submit_evidence called again on ANCHORED row"


def test_fabric_gateway_error_transitions_to_pending_or_failed(db, device, cr):
    """A transient FabricGatewayError on submit_pending: the row should stay
    PENDING (or transition to FAILED after FABRIC_MAX_RETRIES). The error
    is re-raised so the Celery task can schedule a retry."""
    # Create a PENDING row manually.
    record = BlockchainEvidence(
        evidence_id=f"EV-{uuid.uuid4().hex[:10].upper()}",
        change_request_id=cr.id,
        device_id=device.id,
        evidence_type=EvidenceType.CHANGE_VALIDATION,
        evidence_hash="sha256:" + "a" * 64,
        anchor_status=AnchorStatus.PENDING,
        actor_subject="admin@test.com",
        evidence_body={"result": "pass"},
    )
    db.add(record)
    db.commit()

    with patch("app.core.config.settings.FABRIC_MAX_RETRIES", 3), \
         patch(
             "app.services.fabric_gateway_client.submit_evidence",
             side_effect=FabricGatewayError("sidecar unreachable", status_code=503),
         ):
        with pytest.raises(FabricGatewayError):
            fabric_service.submit_pending(db, record)

    db.refresh(record)
    # After one failure with FABRIC_MAX_RETRIES=3 the row should stay PENDING
    # (not FAILED yet -- only exhausted retries → FAILED).
    assert record.anchor_status in (AnchorStatus.PENDING, AnchorStatus.ANCHORING)
    assert record.anchor_error is not None


# ---------------------------------------------------------------------------
# verify_evidence
# ---------------------------------------------------------------------------


def test_verify_evidence_match_fabric_disabled(db, device, cr):
    """With FABRIC_ENABLED=False, verify_evidence falls back to comparing
    the recalculated hash against the stored evidence_hash column.  This
    still detects DB-level tampering (just not ledger-level forgery)."""
    with patch("app.core.config.settings.FABRIC_ENABLED", False):
        record = fabric_service.anchor_evidence(
            db,
            evidence_type=EvidenceType.CHANGE_VALIDATION,
            change_request=cr,
            device=device,
            actor_subject="admin@test.com",
            fields={"result": "pass"},
        )

    db.refresh(record)
    evidence_id = record.evidence_id

    with patch("app.core.config.settings.FABRIC_ENABLED", False):
        result = fabric_service.verify_evidence(db, evidence_id)

    assert result["verified"] is True
    assert "calculated_hash" in result


def test_verify_evidence_mismatch_tampered_body(db, device, cr):
    """If evidence_body is tampered in the DB after anchoring, verify_evidence
    must detect the mismatch by recomputing the hash from the body and
    comparing it to the stored/ledger hash."""
    with patch("app.core.config.settings.FABRIC_ENABLED", False):
        record = fabric_service.anchor_evidence(
            db,
            evidence_type=EvidenceType.CHANGE_VALIDATION,
            change_request=cr,
            device=device,
            actor_subject="admin@test.com",
            fields={"result": "pass"},
        )

    db.refresh(record)
    evidence_id = record.evidence_id

    # Tamper the body (simulating a row-level DB injection).
    record.evidence_body = {**(record.evidence_body or {}), "result": "TAMPERED_TO_PASS"}
    db.commit()

    with patch("app.core.config.settings.FABRIC_ENABLED", False):
        result = fabric_service.verify_evidence(db, evidence_id)

    assert result["verified"] is False
    assert result["calculated_hash"] != result.get("ledger_hash")


# ---------------------------------------------------------------------------
# check_configuration_integrity
# ---------------------------------------------------------------------------


def test_configuration_integrity_check_match():
    config = "hostname router1\ninterface Gi0/1\n ip address 10.0.0.1 255.255.255.0\n!"
    approved_hash = hash_config(config)
    ok, deployment_hash = fabric_service.check_configuration_integrity(approved_hash, config)
    assert ok is True
    assert deployment_hash == approved_hash


def test_configuration_integrity_check_mismatch():
    approved_config = "hostname router1\n!"
    deployed_config = "hostname router1\ninterface Gi0/1\n ip address 10.1.1.1 255.255.255.0\n!"
    approved_hash = hash_config(approved_config)
    ok, deployment_hash = fabric_service.check_configuration_integrity(approved_hash, deployed_config)
    assert ok is False
    assert deployment_hash != approved_hash


def test_configuration_integrity_check_none_approved_hash_returns_false():
    """When approved_hash is None (evidence wasn't anchored at approval time --
    a caller-bug, per the implementation's comment), the gate intentionally
    returns False to alert callers rather than silently deploying. Callers check
    this separately (pipeline_service logs a MISSING_BASELINE event, not TAMPER)."""
    config = "hostname router1\n!"
    ok, _ = fabric_service.check_configuration_integrity(None, config)
    # Intentional per fabric_service.py line 374: "caller bug, not a tamper signal"
    assert ok is False

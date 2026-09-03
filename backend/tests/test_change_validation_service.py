import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.device import Device, DeviceVendor
from app.schemas.change_request import RiskAnalysisResult
from app.services import change_validation_service
from app.services.batfish_service import BatfishResult, BatfishStatus, BehaviorFinding
from app.services.change_validation_service import CombinedDecision
from app.services.opa_service import OpaDecision, OpaResult, OpaViolation
from app.services.validation_engine import ValidationResult


class _FakeChangeRequest:
    """Minimal stand-in with just the attributes change_validation_service
    reads off a ChangeRequest -- avoids needing a full ChangeRequest row
    with all its FKs for orchestrator-level tests (the model itself is
    exercised via test_pipeline.py / the API tests)."""

    def __init__(self, priority="medium"):
        self.id = uuid.uuid4()
        self.description = "test change"
        self.priority = priority
        self.business_justification = None


@pytest.fixture()
def db_session():
    os.environ["NETGUARD_CRED_TEST_ORCH_DEVICE"] = "test-password"
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    os.environ.pop("NETGUARD_CRED_TEST_ORCH_DEVICE", None)


@pytest.fixture()
def device(db_session):
    d = Device(
        hostname="rtr-orch", ip_address="10.0.0.9", vendor=DeviceVendor.CISCO,
        ssh_username="admin", ssh_credential_ref="test-orch-device",
    )
    db_session.add(d)
    db_session.commit()
    db_session.refresh(d)
    return d


def _risk(score=10, classification="Low Risk"):
    return RiskAnalysisResult(risk_score=score, classification=classification, recommendation="", findings=[])


PATCH_TARGET = "app.services.change_validation_service"


async def _run(db, device, cr, *, syntax_passed=True, opa_decision=OpaDecision.ALLOW,
                batfish_status=BatfishStatus.PASS, risk_score=10, risk_classification="Low Risk"):
    syntax_result = ValidationResult(passed=syntax_passed, errors=[] if syntax_passed else ["bad config"])
    opa_result = OpaResult(
        passed=opa_decision != OpaDecision.DENY,
        decision=opa_decision,
        violations=[OpaViolation("some.policy", "critical", "denied")] if opa_decision == OpaDecision.DENY else [],
    )
    batfish_result = BatfishResult(
        status=batfish_status,
        findings=[BehaviorFinding("q", "src", "dst", None, None, "DENIED", "ACCEPTED", True, "critical", "bad")]
        if batfish_status == BatfishStatus.CRITICAL else [],
    )

    with patch(f"{PATCH_TARGET}.validation_engine.validate_syntax", return_value=syntax_result), \
         patch(f"{PATCH_TARGET}.risk_engine.analyze", return_value=_risk(risk_score, risk_classification)), \
         patch(f"{PATCH_TARGET}.impact_simulation_service.simulate_impact") as mock_impact, \
         patch(f"{PATCH_TARGET}.opa_service.evaluate_change", new_callable=AsyncMock) as mock_opa, \
         patch(f"{PATCH_TARGET}.batfish_service.validate_configuration", new_callable=AsyncMock) as mock_batfish:
        mock_impact.return_value = type("_Impact", (), {"isolated_devices": [], "degraded_devices": []})()
        mock_opa.return_value = opa_result
        mock_batfish.return_value = batfish_result
        return await change_validation_service.validate_change(
            db, device=device, change_request=cr, current_config="!\n", proposed_config="interface Gi0/1\n",
        ), mock_opa, mock_batfish


@pytest.mark.asyncio
async def test_syntax_failure_blocks_before_opa_or_batfish_called(db_session, device):
    cr = _FakeChangeRequest()
    result, mock_opa, mock_batfish = await _run(db_session, device, cr, syntax_passed=False)
    assert result.decision == CombinedDecision.BLOCK
    assert result.syntax_passed is False
    mock_opa.assert_not_called()
    mock_batfish.assert_not_called()


@pytest.mark.asyncio
async def test_opa_deny_blocks_even_with_batfish_pass(db_session, device):
    cr = _FakeChangeRequest()
    result, _, _ = await _run(db_session, device, cr, opa_decision=OpaDecision.DENY, batfish_status=BatfishStatus.PASS)
    assert result.decision == CombinedDecision.BLOCK


@pytest.mark.asyncio
async def test_batfish_critical_blocks_even_with_opa_allow(db_session, device):
    cr = _FakeChangeRequest()
    result, _, _ = await _run(db_session, device, cr, opa_decision=OpaDecision.ALLOW, batfish_status=BatfishStatus.CRITICAL)
    assert result.decision == CombinedDecision.BLOCK


@pytest.mark.asyncio
async def test_high_risk_reviews_even_when_opa_and_batfish_pass(db_session, device):
    cr = _FakeChangeRequest()
    result, _, _ = await _run(
        db_session, device, cr, opa_decision=OpaDecision.ALLOW, batfish_status=BatfishStatus.PASS,
        risk_score=85, risk_classification="Critical Risk",
    )
    assert result.decision == CombinedDecision.REVIEW


@pytest.mark.asyncio
async def test_opa_review_downgrades_to_review_not_pass(db_session, device):
    cr = _FakeChangeRequest()
    result, _, _ = await _run(db_session, device, cr, opa_decision=OpaDecision.REVIEW, batfish_status=BatfishStatus.PASS)
    assert result.decision == CombinedDecision.REVIEW


@pytest.mark.asyncio
async def test_everything_passes_yields_pass(db_session, device):
    cr = _FakeChangeRequest()
    result, _, _ = await _run(db_session, device, cr)
    assert result.decision == CombinedDecision.PASS


@pytest.mark.asyncio
async def test_batfish_unavailable_with_high_risk_reviews_not_silently_passes(db_session, device):
    cr = _FakeChangeRequest()
    result, _, _ = await _run(
        db_session, device, cr, batfish_status=BatfishStatus.UNAVAILABLE,
        risk_score=90, risk_classification="Critical Risk",
    )
    assert result.decision == CombinedDecision.REVIEW


@pytest.mark.asyncio
async def test_batfish_unavailable_fail_closed_reviews_even_at_low_risk(db_session, device):
    """BATFISH_FAIL_CLOSED=true must not depend on risk score -- an
    unavailable Batfish is treated conservatively (REVIEW) on its own,
    not silently PASSed just because risk happened to be low."""
    with patch(f"{PATCH_TARGET}.batfish_service.fail_closed", True):
        result, _, _ = await _run(
            db_session, device, cr := _FakeChangeRequest(), batfish_status=BatfishStatus.UNAVAILABLE,
            risk_score=5, risk_classification="Low Risk",
        )
    assert result.decision == CombinedDecision.REVIEW


@pytest.mark.asyncio
async def test_batfish_unavailable_fail_open_low_risk_passes(db_session, device):
    """Conversely, with BATFISH_FAIL_CLOSED=false and low risk, an
    unavailable Batfish should not block a routine change."""
    with patch(f"{PATCH_TARGET}.batfish_service.fail_closed", False):
        result, _, _ = await _run(
            db_session, device, cr := _FakeChangeRequest(), batfish_status=BatfishStatus.UNAVAILABLE,
            risk_score=5, risk_classification="Low Risk",
        )
    assert result.decision == CombinedDecision.PASS


@pytest.mark.asyncio
async def test_device_role_reads_device_role_column(db_session):
    """device_dict['role'] must reflect Device.device_role -- OPA's
    core-device policies (change_management.core_device_stricter_review,
    routing.core_routing_change_elevated_approval) depend on this being
    populated, not silently 'unknown' for every real device."""
    device = Device(
        hostname="core-rtr-1", ip_address="10.0.0.5", vendor=DeviceVendor.CISCO,
        ssh_username="admin", ssh_credential_ref="test-role-device", device_role="core",
    )
    db_session.add(device)
    db_session.commit()
    db_session.refresh(device)
    cr = _FakeChangeRequest()

    captured = {}

    async def _fake_opa_evaluate(**kwargs):
        captured["role"] = kwargs["device"]["role"]
        return OpaResult(passed=True, decision=OpaDecision.ALLOW)

    syntax_result = ValidationResult(passed=True, errors=[])
    with patch(f"{PATCH_TARGET}.validation_engine.validate_syntax", return_value=syntax_result), \
         patch(f"{PATCH_TARGET}.risk_engine.analyze", return_value=_risk()), \
         patch(f"{PATCH_TARGET}.impact_simulation_service.simulate_impact") as mock_impact, \
         patch(f"{PATCH_TARGET}.opa_service.evaluate_change", side_effect=_fake_opa_evaluate), \
         patch(f"{PATCH_TARGET}.batfish_service.validate_configuration", new_callable=AsyncMock) as mock_batfish:
        mock_impact.return_value = type("_Impact", (), {"isolated_devices": [], "degraded_devices": []})()
        mock_batfish.return_value = BatfishResult(status=BatfishStatus.PASS)
        await change_validation_service.validate_change(
            db_session, device=device, change_request=cr, current_config="!\n", proposed_config="interface Gi0/1\n",
        )
    assert captured["role"] == "core"


@pytest.mark.asyncio
async def test_lower_severity_pass_never_overrides_earlier_block(db_session, device):
    """A DENY from OPA combined with a PASS from Batfish and low risk must
    still BLOCK -- the highest-severity result wins regardless of order or
    what any other engine says (spec Section 12)."""
    cr = _FakeChangeRequest()
    result, _, _ = await _run(
        db_session, device, cr, opa_decision=OpaDecision.DENY, batfish_status=BatfishStatus.PASS,
        risk_score=5, risk_classification="Low Risk",
    )
    assert result.decision == CombinedDecision.BLOCK

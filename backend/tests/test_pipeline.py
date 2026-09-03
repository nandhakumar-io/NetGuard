import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.change_request import ChangePriority, ChangeRequest, ChangeStatus
from app.models.device import Device, DeviceVendor
from app.schemas.device_job import DeviceJobResult
from app.services import pipeline_service
from app.services.change_validation_service import (
    ChangeValidationResult,
    CombinedDecision,
)
from app.services.device_job_service import DeviceJobFailedError
from app.services.health_monitor import CheckOutcome, MonitoringResult, PollRound


def _job_result(*, success=True, output="ok", error=None, protocol="ssh") -> DeviceJobResult:
    """Builds a DeviceJobResult the same shape device_job_service.submit_job_sync
    returns after a real round trip through the Device Gateway -- both the
    pre-deploy GET_RUNNING_CONFIG re-validation read and the DEPLOY_CONFIG/
    ROLLBACK_CONFIG pushes go through this single call now (see
    pipeline_service.py Section 3/8 migration), so every deploy-path test
    mocks this one function instead of the old in-process ProtocolManager."""
    return DeviceJobResult(
        job_id="test-job", success=success, output=output, error=error,
        executed_at=datetime.now(timezone.utc).isoformat(), protocol=protocol,
        execution_time_ms=10.0,
    )


def _monitoring_result(outcomes: list[CheckOutcome], healthy: bool, rounds: int = 1) -> MonitoringResult:
    """Builds a MonitoringResult with `rounds` identical poll rounds, for
    tests that don't care about multi-round polling specifically -- see
    test_health_monitor.py for tests that do.
    """
    poll_rounds = [PollRound(i + 1, i * 15, outcomes, healthy) for i in range(rounds)]
    return MonitoringResult(healthy=healthy, rounds=poll_rounds, window_seconds=rounds * 15, poll_interval_seconds=15)


@pytest.fixture()
def db_session():
    os.environ["NETGUARD_CRED_TEST_PIPELINE_DEVICE"] = "test-password"
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    os.environ.pop("NETGUARD_CRED_TEST_PIPELINE_DEVICE", None)


def _pass_validation() -> ChangeValidationResult:
    """A PASS result from the OPA/Batfish gate in
    pipeline_service.run_deployment_pipeline -- every pre-existing deploy-
    path test mocks change_validation_service.validate_change to this so
    it isolates the specific behavior under test (deploy success/failure,
    rollback) from OPA/Batfish, which have their own dedicated tests in
    test_change_validation_service.py and are exercised directly by
    test_pipeline_final_gate.py.
    """
    return ChangeValidationResult(
        decision=CombinedDecision.PASS,
        overall_score=10,
        syntax_passed=True,
        syntax_errors=[],
        syntax_warnings=[],
        opa=None,
        batfish=None,
        risk_score=10,
        risk_classification="Low Risk",
        blast_radius_devices=0,
        reasons=[],
    )


def _make_cr(db):
    device = Device(
        hostname="rtr-01",
        ip_address="10.0.0.1",
        vendor=DeviceVendor.CISCO,
        ssh_username="admin",
        ssh_credential_ref="test-pipeline-device",
    )
    db.add(device)
    db.commit()
    db.refresh(device)

    cr = ChangeRequest(
        device_id=device.id,
        submitted_by=device.id,  # placeholder FK-agnostic value for the sqlite test db
        priority=ChangePriority.MEDIUM,
        description="test change",
        proposed_config="interface Gi0/1\n ip address 10.2.2.1 255.255.255.0\n",
        status=ChangeStatus.APPROVED,
    )
    db.add(cr)
    db.commit()
    db.refresh(cr)
    return cr


@patch("app.services.pipeline_service.event_bus.publish_event")
@patch("app.services.pipeline_service.change_validation_service.validate_change", new_callable=AsyncMock)
@patch("app.services.pipeline_service.notification_service.notify")
@patch("app.services.pipeline_service.health_monitor.run_monitoring_window")
@patch("app.services.pipeline_service.device_job_service.submit_job_sync")
def test_pipeline_success_path(mock_submit, mock_health, mock_notify, mock_validate, mock_publish, db_session):
    # event_bus.publish_event is mocked here (rather than left to hit the
    # real event_bus module) because it isn't a no-op in tests: the first
    # call lazily starts a persistent background thread that tries a real
    # NATS connection (see app.services.event_bus._PublisherLoop). Left
    # unmocked, a failed connection attempt in an environment without a
    # reachable NATS broker (socket.gaierror) doesn't fail this test
    # directly, but leaks a daemon thread that retries forever in the
    # background and can intermittently break unrelated tests running
    # later in the same session -- see tests/conftest.py's autouse
    # `_reset_event_bus_publisher` fixture for the session-level backstop.
    cr = _make_cr(db_session)

    # Three Gateway job round trips on the happy path: the pre-deploy
    # GET_RUNNING_CONFIG re-validation read, the DEPLOY_CONFIG push, and
    # -- Section 13's mandatory post-deployment verification -- a second
    # GET_RUNNING_CONFIG read after a healthy deploy, comparing its hash
    # against the approved config (see pipeline_service.run_deployment_for_device,
    # "Post-deployment verification (Section 13, mandatory)"). This third
    # call only fires on the healthy path -- test_pipeline_rolls_back_on_failed_health_check
    # doesn't need a third mocked result because it never reaches it.
    # Returning the exact proposed_config here means the hash comparison
    # matches and no POST_DEPLOYMENT_CONFIGURATION_MISMATCH evidence is
    # anchored, keeping this test's assertions about the clean-success path.
    mock_submit.side_effect = [
        _job_result(output="! current config"),
        _job_result(),
        _job_result(output=cr.proposed_config),
    ]
    mock_validate.return_value = _pass_validation()
    outcomes = [CheckOutcome("infrastructure", "ping", True, "2 packets received")]
    mock_health.return_value = _monitoring_result(outcomes, healthy=True, rounds=3)

    result = pipeline_service.run_deployment_pipeline(db_session, cr, actor_email="admin@netguard.ai")

    assert result.status == ChangeStatus.SUCCESS
    deployment = db_session.query(pipeline_service.Deployment).filter(
        pipeline_service.Deployment.change_request_id == cr.id
    ).first()
    mock_notify.assert_called_with(
        "Deployment Succeeded", "rtr-01: change deployed and healthy.", severity="info",
        device_hostname="rtr-01", change_request_id=cr.id, deployment_id=deployment.id,
    )
    # All 3 poll rounds' checks should have been persisted, not just one.
    checks = db_session.query(pipeline_service.HealthCheckResult).filter(
        pipeline_service.HealthCheckResult.deployment_id == deployment.id
    ).all()
    assert len(checks) == 3
    assert {c.poll_round for c in checks} == {1, 2, 3}


@patch("app.services.pipeline_service.event_bus.publish_event")
@patch("app.services.pipeline_service.change_validation_service.validate_change", new_callable=AsyncMock)
@patch("app.services.pipeline_service.notification_service.notify")
@patch("app.services.pipeline_service.health_monitor.run_monitoring_window")
@patch("app.services.pipeline_service.device_job_service.submit_job_sync")
def test_pipeline_rolls_back_on_failed_health_check(
    mock_submit, mock_health, mock_notify, mock_validate, mock_publish, db_session
):
    # Self-healing rollback re-pushes the pre-flight snapshot's config via
    # a separate ROLLBACK_CONFIG Gateway job -- three Gateway round trips
    # total on this path: GET_RUNNING_CONFIG (pre-deploy re-validation),
    # DEPLOY_CONFIG (the original change), ROLLBACK_CONFIG (the restore
    # push after the health check fails), all mocked to succeed here so
    # the test isolates the health-check-triggers-rollback behavior.
    mock_submit.side_effect = [
        _job_result(output="! current config"), _job_result(), _job_result(),
    ]
    mock_validate.return_value = _pass_validation()
    outcomes = [CheckOutcome("infrastructure", "ping", False, "timeout")]
    mock_health.return_value = _monitoring_result(outcomes, healthy=False, rounds=1)

    cr = _make_cr(db_session)
    result = pipeline_service.run_deployment_pipeline(db_session, cr, actor_email="admin@netguard.ai")

    assert result.status == ChangeStatus.ROLLED_BACK
    # Called for: GET_RUNNING_CONFIG re-validation, the original
    # DEPLOY_CONFIG, and the ROLLBACK_CONFIG restore after the health
    # check failed.
    assert mock_submit.call_count == 3
    ops = [call.kwargs["operation"] for call in mock_submit.call_args_list]
    assert ops[-1] == "rollback_config"


@patch("app.services.pipeline_service.event_bus.publish_event")
@patch("app.services.pipeline_service.change_validation_service.validate_change", new_callable=AsyncMock)
@patch("app.services.pipeline_service.notification_service.notify")
@patch("app.services.pipeline_service.device_job_service.submit_job_sync")
def test_pipeline_marks_failed_when_deploy_fails(mock_submit, mock_notify, mock_validate, mock_publish, db_session):
    # GET_RUNNING_CONFIG re-validation succeeds; the DEPLOY_CONFIG push
    # fails -- device_job_service raises DeviceJobFailedError rather than
    # returning success=False, mirroring what a real rejected/failed
    # Gateway job looks like from the caller's side.
    mock_submit.side_effect = [_job_result(output="! current config"), DeviceJobFailedError("auth failure")]
    mock_validate.return_value = _pass_validation()

    cr = _make_cr(db_session)
    result = pipeline_service.run_deployment_pipeline(db_session, cr, actor_email="admin@netguard.ai")

    assert result.status == ChangeStatus.FAILED


@patch("app.services.pipeline_service.event_bus.publish_event")
@patch("app.services.pipeline_service.change_validation_service.validate_change", new_callable=AsyncMock)
@patch("app.services.pipeline_service.notification_service.notify")
@patch("app.services.pipeline_service.device_job_service.submit_job_sync")
def test_pipeline_blocks_deploy_when_opa_batfish_gate_blocks(
    mock_submit, mock_notify, mock_validate, mock_publish, db_session
):
    """Core safety guarantee (spec Section 17/25): a BLOCK decision from
    the final OPA/Batfish gate must prevent DEPLOY_CONFIG from ever being
    submitted -- not just be reflected in a decision object. Only the
    GET_RUNNING_CONFIG pre-validation read should happen; the deploy call
    must never occur.
    """
    mock_submit.side_effect = [_job_result(output="! current config")]
    mock_validate.return_value = ChangeValidationResult(
        decision=CombinedDecision.BLOCK,
        overall_score=95,
        syntax_passed=True,
        syntax_errors=[],
        syntax_warnings=[],
        opa=None,
        batfish=None,
        risk_score=95,
        risk_classification="Critical Risk",
        blast_radius_devices=10,
        reasons=["OPA policy evaluation denied this change"],
    )

    cr = _make_cr(db_session)
    result = pipeline_service.run_deployment_pipeline(db_session, cr, actor_email="admin@netguard.ai")

    assert result.status == ChangeStatus.FAILED
    # Exactly one Gateway call (the pre-validation config read) -- deploy
    # must never be submitted after a BLOCK.
    assert mock_submit.call_count == 1
    ops = [call.kwargs.get("operation") for call in mock_submit.call_args_list]
    assert "deploy_config" not in [str(o).lower() for o in ops]
    from app.schemas.device_job import DeviceOperation
    assert all(op != DeviceOperation.DEPLOY_CONFIG for op in ops)

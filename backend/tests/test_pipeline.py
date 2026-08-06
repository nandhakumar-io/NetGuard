import os
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.change_request import ChangePriority, ChangeRequest, ChangeStatus
from app.models.device import Device, DeviceVendor
from app.models.protocol_operation import ProtocolName
from app.services import pipeline_service
from app.services.protocol_manager import ProtocolResult
from app.services.health_monitor import CheckOutcome, MonitoringResult, PollRound


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


@patch("app.services.pipeline_service.notification_service.notify")
@patch("app.services.pipeline_service.health_monitor.run_monitoring_window")
@patch("app.services.pipeline_service.protocol_manager.ProtocolManager.deploy_config")
def test_pipeline_success_path(mock_deploy, mock_health, mock_notify, db_session):
    mock_deploy.return_value = ProtocolResult(
        success=True, protocol=ProtocolName.NETCONF, operation="deploy_config",
        output="ok", error=None, execution_time_ms=10.0, correlation_id="123"
    )
    outcomes = [CheckOutcome("infrastructure", "ping", True, "2 packets received")]
    mock_health.return_value = _monitoring_result(outcomes, healthy=True, rounds=3)

    cr = _make_cr(db_session)
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


@patch("app.services.pipeline_service.notification_service.notify")
@patch("app.services.pipeline_service.health_monitor.run_monitoring_window")
@patch("app.services.pipeline_service.protocol_manager.ProtocolManager.deploy_config")
def test_pipeline_rolls_back_on_failed_health_check(
    mock_deploy, mock_health, mock_notify, db_session
):
    # Self-healing rollback re-pushes the pre-flight snapshot's config via
    # the same deploy path used for the original change (ProtocolManager.
    # restore_config() -> deploy_config()) -- there's no separate
    # "rollback_config" call in this pipeline, so deploy_config is mocked
    # to succeed for *both* the initial deploy and the restore push.
    mock_deploy.return_value = ProtocolResult(
        success=True, protocol=ProtocolName.NETCONF, operation="deploy_config",
        output="ok", error=None, execution_time_ms=10.0, correlation_id="123"
    )
    outcomes = [CheckOutcome("infrastructure", "ping", False, "timeout")]
    mock_health.return_value = _monitoring_result(outcomes, healthy=False, rounds=1)

    cr = _make_cr(db_session)
    result = pipeline_service.run_deployment_pipeline(db_session, cr, actor_email="admin@netguard.ai")

    assert result.status == ChangeStatus.ROLLED_BACK
    # Called once for the original deploy, once more to restore the
    # pre-flight snapshot after the health check failed.
    assert mock_deploy.call_count == 2


@patch("app.services.pipeline_service.notification_service.notify")
@patch("app.services.pipeline_service.protocol_manager.ProtocolManager.deploy_config")
def test_pipeline_marks_failed_when_deploy_fails(mock_deploy, mock_notify, db_session):
    mock_deploy.return_value = ProtocolResult(
        success=False, protocol=ProtocolName.NETCONF, operation="deploy_config",
        output="", error="auth failure", execution_time_ms=10.0, correlation_id="123"
    )

    cr = _make_cr(db_session)
    result = pipeline_service.run_deployment_pipeline(db_session, cr, actor_email="admin@netguard.ai")

    assert result.status == ChangeStatus.FAILED

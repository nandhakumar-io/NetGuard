from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.device import Device, DeviceVendor
from app.models.change_request import ChangeRequest, ChangeStatus, ChangePriority
from app.services import pipeline_service
from app.services.deployment_engine import DeployResult
from app.services.health_monitor import CheckOutcome


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _make_cr(db):
    device = Device(hostname="rtr-01", ip_address="10.0.0.1", vendor=DeviceVendor.CISCO)
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
@patch("app.services.pipeline_service.health_monitor.run_health_suite")
@patch("app.services.pipeline_service.deployment_engine.deploy_config")
def test_pipeline_success_path(mock_deploy, mock_health, mock_notify, db_session):
    mock_deploy.return_value = DeployResult(success=True, output="ok")
    mock_health.return_value = [CheckOutcome("infrastructure", "ping", True, "2 packets received")]

    cr = _make_cr(db_session)
    result = pipeline_service.run_deployment_pipeline(db_session, cr, actor_email="admin@netguard.ai")

    assert result.status == ChangeStatus.SUCCESS
    mock_notify.assert_called_with(
        "Deployment Succeeded", "rtr-01: change deployed and healthy.", severity="info"
    )


@patch("app.services.pipeline_service.notification_service.notify")
@patch("app.services.pipeline_service.deployment_engine.rollback_config")
@patch("app.services.pipeline_service.health_monitor.run_health_suite")
@patch("app.services.pipeline_service.deployment_engine.deploy_config")
def test_pipeline_rolls_back_on_failed_health_check(
    mock_deploy, mock_health, mock_rollback, mock_notify, db_session
):
    mock_deploy.return_value = DeployResult(success=True, output="ok")
    mock_health.return_value = [CheckOutcome("infrastructure", "ping", False, "timeout")]
    mock_rollback.return_value = DeployResult(success=True, output="restored")

    cr = _make_cr(db_session)
    result = pipeline_service.run_deployment_pipeline(db_session, cr, actor_email="admin@netguard.ai")

    assert result.status == ChangeStatus.ROLLED_BACK
    mock_rollback.assert_called_once()


@patch("app.services.pipeline_service.notification_service.notify")
@patch("app.services.pipeline_service.deployment_engine.deploy_config")
def test_pipeline_marks_failed_when_deploy_fails(mock_deploy, mock_notify, db_session):
    mock_deploy.return_value = DeployResult(success=False, output="", error="auth failure")

    cr = _make_cr(db_session)
    result = pipeline_service.run_deployment_pipeline(db_session, cr, actor_email="admin@netguard.ai")

    assert result.status == ChangeStatus.FAILED

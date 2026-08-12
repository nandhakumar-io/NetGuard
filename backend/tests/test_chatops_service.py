import uuid
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.alert import Alert, AlertSeverity, AlertSource
from app.models.change_request import ChangePriority, ChangeRequest, ChangeStatus
from app.models.device import Device, DeviceStatus, DeviceVendor
from app.models.user import User, UserRole
from app.services import chatops_service


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _make_device(db, hostname="rtr-02", status=DeviceStatus.ONLINE):
    device = Device(
        hostname=hostname,
        ip_address="10.0.0.2",
        vendor=DeviceVendor.CISCO,
        ssh_username="admin",
        status=status,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


def _make_user(db, email="admin@netguard.ai", role=UserRole.NETWORK_ADMIN):
    user = User(
        email=email,
        full_name="Test User",
        hashed_password="x",
        role=role,
        slack_user_id="U12345",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_alert(db, device, category="High CPU", severity=AlertSeverity.CRITICAL, resolved=False):
    alert = Alert(
        device_id=device.id,
        severity=severity,
        source=AlertSource.HEALTH_POLL,
        category=category,
        message="Test alert",
        resolved=resolved,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return alert


def _make_change_request(db, device, status=ChangeStatus.PENDING_APPROVAL):
    cr = ChangeRequest(
        device_id=device.id,
        submitted_by=uuid.uuid4(),
        priority=ChangePriority.MEDIUM,
        description="test",
        proposed_config="test",
        status=status,
    )
    db.add(cr)
    db.commit()
    db.refresh(cr)
    return cr


def test_command_help(db_session):
    user = _make_user(db_session)
    result = chatops_service.execute_command(db_session, user, "help")
    assert result.ok is True
    assert "NetGuard ChatOps commands" in result.text
    assert "approve" in result.text
    assert "status <hostname>" in result.text


def test_command_unknown(db_session):
    user = _make_user(db_session)
    result = chatops_service.execute_command(db_session, user, "launch-missiles")
    assert result.ok is False
    assert "Unknown command 'launch-missiles'" in result.text


def test_status_device_not_found(db_session):
    user = _make_user(db_session)
    result = chatops_service.execute_command(db_session, user, "status imaginary-rtr")
    assert result.ok is False
    assert "No device found" in result.text


@patch("app.core.vm_client.latest_device_metrics")
def test_status_device_success(mock_metrics, db_session):
    mock_metrics.return_value = {"health_color": "green", "uptime_seconds": 3600}
    device = _make_device(db_session)
    user = _make_user(db_session)

    result = chatops_service.execute_command(db_session, user, f"status {device.hostname}")

    assert result.ok is True
    assert device.hostname in result.text
    assert "Health: *green*" in result.text
    assert "Uptime: 1h 0m" in result.text


@patch("app.services.alert_service.acknowledge_alert")
def test_ack_alert_success(mock_ack, db_session):
    user = _make_user(db_session)
    device = _make_device(db_session)
    alert = _make_alert(db_session, device)

    mock_alert_obj = MagicMock()
    mock_alert_obj.id = alert.id
    mock_alert_obj.category = alert.category
    mock_ack.return_value = mock_alert_obj

    result = chatops_service.execute_command(db_session, user, f"ack {alert.id}")

    assert result.ok is True
    assert "acknowledged by" in result.text
    mock_ack.assert_called_once_with(db_session, alert.id, user.email)


def test_ack_invalid_uuid(db_session):
    user = _make_user(db_session)
    result = chatops_service.execute_command(db_session, user, "ack not-a-uuid")
    assert result.ok is False
    assert "doesn't look like a valid alert ID" in result.text


@patch("app.services.alert_service.resolve_alert")
def test_resolve_alert_success(mock_resolve, db_session):
    user = _make_user(db_session)
    device = _make_device(db_session)
    alert = _make_alert(db_session, device)

    mock_alert_obj = MagicMock()
    mock_alert_obj.id = alert.id
    mock_alert_obj.category = alert.category
    mock_resolve.return_value = mock_alert_obj

    result = chatops_service.execute_command(db_session, user, f"resolve {alert.id}")

    assert result.ok is True
    assert "resolved by" in result.text
    mock_resolve.assert_called_once_with(db_session, alert.id, user.email)


def test_alerts_device_specific(db_session):
    device = _make_device(db_session)
    _make_alert(db_session, device, severity=AlertSeverity.CRITICAL)
    _make_alert(db_session, device, severity=AlertSeverity.INFO)
    user = _make_user(db_session)

    result = chatops_service.execute_command(db_session, user, f"alerts {device.hostname}")

    assert result.ok is True
    assert result.severity == "critical"
    # Ensure items are populated for rich UI handling downstream
    assert len(result.items) == 2
    assert "critical" in result.text
    assert "info" in result.text


@patch("app.services.metrics_service.fleet_health_summary")
def test_fleet_command(mock_fleet, db_session):
    mock_fleet.return_value = {
        "green": 10, "yellow": 2, "red": 0, "unknown": 1,
        "devices_monitored": 13, "average_health_score": 95,
        "devices_with_stale_metrics": 0
    }
    user = _make_user(db_session)

    result = chatops_service.execute_command(db_session, user, "fleet")

    assert result.ok is True
    assert "Green: *10*" in result.text
    assert "Yellow: *2*" in result.text
    assert result.severity == "warning"


@patch("app.services.drift_service.fleet_summary")
def test_drift_fleet_command(mock_drift_fleet, db_session):
    mock_drift_fleet.return_value = {
        "total_open_drifts": 3,
        "devices_drifted": 2,
        "average_compliance_score": 85,
        "by_severity": {"high": 1, "medium": 2},
        "rollback_recommended_count": 0
    }
    user = _make_user(db_session)

    result = chatops_service.execute_command(db_session, user, "drift")

    assert result.ok is True
    assert "Open drifts: *3*" in result.text
    assert result.severity == "warning"


@patch("app.api.config_management.backup_config")
def test_backup_command_success(mock_backup, db_session):
    mock_result = MagicMock()
    mock_result.message = "Backup completed."
    mock_backup.return_value = mock_result

    device = _make_device(db_session)
    user = _make_user(db_session, role=UserRole.NETWORK_ADMIN)

    result = chatops_service.execute_command(db_session, user, f"backup {device.hostname}")

    assert result.ok is True
    assert "Backup completed." in result.text
    mock_backup.assert_called_once_with(device.id, payload=None, db=db_session, current_user=user)


def test_backup_command_rbac(db_session):
    device = _make_device(db_session)
    # NOC_ENGINEER role should not be allowed
    user = _make_user(db_session, role=UserRole.NOC_ENGINEER)

    result = chatops_service.execute_command(db_session, user, f"backup {device.hostname}")

    assert result.ok is False
    assert "Only Network Administrators" in result.text


@patch("app.services.device_overview_service.build_device_overview")
def test_whois_command(mock_overview, db_session):
    device = _make_device(db_session)
    user = _make_user(db_session)

    mock_overview.return_value = {
        "hostname": device.hostname,
        "vendor": "CISCO",
        "status": "online",
        "health": {"health_color": "green", "health_score": 100},
        "active_alert_count": 0,
        "window_hours": 24,
        "drift_count": 0,
        "notable_syslog_count": 0,
        "deployment_count": 0,
        "timeline": []
    }

    result = chatops_service.execute_command(db_session, user, f"whois {device.hostname}")

    assert result.ok is True
    assert device.hostname in result.text
    assert "Health: *green* (100)" in result.text
    assert result.severity == "info"

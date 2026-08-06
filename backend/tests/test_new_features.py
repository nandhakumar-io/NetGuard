"""Tests for:
  - drift_service.weekly_golden_config_drift (one-click "drifted from
    golden config this week" report)
  - snapshot_service.purge_expired_snapshots / retention_status_for_device
    (scheduled-snapshot retention policy)
  - rollback_service.preview_rollback (rollback diff preview)
"""
import datetime
import os
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.change_request import ChangeRequest, ChangePriority, ChangeStatus
from app.models.config_drift import ConfigDrift, DriftBaseline, DriftSeverity, DriftStatus
from app.models.device import Device, DeviceVendor
from app.models.snapshot import ConfigSnapshot
from app.models.user import User, UserRole
from app.services import drift_service, rollback_service, snapshot_service


@pytest.fixture()
def db_session():
    os.environ["NETGUARD_CRED_TEST_ROLLBACK_DEVICE"] = "test-password"
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    os.environ.pop("NETGUARD_CRED_TEST_ROLLBACK_DEVICE", None)


def _make_device(db, hostname="rtr-01", cred_ref="test-rollback-device"):
    device = Device(
        hostname=hostname,
        ip_address="10.0.0.9",
        vendor=DeviceVendor.CISCO,
        ssh_username="admin",
        ssh_credential_ref=cred_ref,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


def _make_snapshot(db, device, config="interface Gi0/1\n", version="1"):
    payload = snapshot_service.build_snapshot_payload(running_config=config, startup_config=config, version=version)
    snap = ConfigSnapshot(device_id=device.id, seq=snapshot_service.next_seq(db), **payload)
    db.add(snap)
    db.commit()
    db.refresh(snap)
    return snap


def _make_admin(db):
    user = User(email="admin@netguard.ai", full_name="Admin", hashed_password="x", role=UserRole.NETWORK_ADMIN)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_drift(db, device, baseline, severity, added=3, removed=1, days_ago=1):
    drift = ConfigDrift(
        device_id=device.id,
        baseline=baseline,
        diff_text="--- a\n+++ b\n+interface Gi0/1\n",
        added_lines=added,
        removed_lines=removed,
        modified_lines=0,
        risk_score=50,
        compliance_score=70,
        severity=severity,
        status=DriftStatus.OPEN,
    )
    db.add(drift)
    db.commit()
    db.refresh(drift)
    # detected_at has a server_default, so backdate it explicitly for the
    # "this week" window test.
    drift.detected_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)
    db.commit()
    db.refresh(drift)
    return drift


# ---------------------------------------------------------------------------
# weekly_golden_config_drift
# ---------------------------------------------------------------------------
def test_weekly_golden_config_drift_dedupes_to_latest_per_device(db_session):
    device = _make_device(db_session)
    _make_drift(db_session, device, DriftBaseline.GOLDEN_CONFIG, DriftSeverity.LOW, days_ago=5)
    newest = _make_drift(db_session, device, DriftBaseline.GOLDEN_CONFIG, DriftSeverity.HIGH, days_ago=1)

    results = drift_service.weekly_golden_config_drift(db_session, days=7)

    assert len(results) == 1
    assert results[0].id == newest.id


def test_weekly_golden_config_drift_excludes_non_golden_baseline(db_session):
    device = _make_device(db_session)
    _make_drift(db_session, device, DriftBaseline.PREVIOUS_BACKUP, DriftSeverity.CRITICAL, days_ago=1)

    results = drift_service.weekly_golden_config_drift(db_session, days=7)

    assert results == []


def test_weekly_golden_config_drift_excludes_outside_window(db_session):
    device = _make_device(db_session)
    _make_drift(db_session, device, DriftBaseline.GOLDEN_CONFIG, DriftSeverity.HIGH, days_ago=30)

    results = drift_service.weekly_golden_config_drift(db_session, days=7)

    assert results == []


def test_weekly_golden_config_drift_excludes_no_op_scans(db_session):
    device = _make_device(db_session)
    _make_drift(db_session, device, DriftBaseline.GOLDEN_CONFIG, DriftSeverity.LOW, added=0, removed=0, days_ago=1)

    results = drift_service.weekly_golden_config_drift(db_session, days=7)

    assert results == []


def test_weekly_golden_config_drift_sorts_severity_first(db_session):
    device_a = _make_device(db_session, hostname="rtr-low")
    device_b = _make_device(db_session, hostname="rtr-critical")
    _make_drift(db_session, device_a, DriftBaseline.GOLDEN_CONFIG, DriftSeverity.LOW, days_ago=1)
    critical = _make_drift(db_session, device_b, DriftBaseline.GOLDEN_CONFIG, DriftSeverity.CRITICAL, days_ago=2)

    results = drift_service.weekly_golden_config_drift(db_session, days=7)

    assert results[0].id == critical.id


# ---------------------------------------------------------------------------
# Snapshot retention
# ---------------------------------------------------------------------------
def _backdate(snapshot, days_ago, db):
    snapshot.created_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)
    db.commit()


def test_purge_expired_snapshots_keeps_min_per_device_regardless_of_age(db_session):
    device = _make_device(db_session)
    snaps = [_make_snapshot(db_session, device, version=str(i)) for i in range(3)]
    for s in snaps:
        _backdate(s, days_ago=200, db=db_session)

    with patch("app.core.config.settings.SNAPSHOT_RETENTION_MIN_PER_DEVICE", 3), \
         patch("app.core.config.settings.SNAPSHOT_RETENTION_DAYS", 90):
        result = snapshot_service.purge_expired_snapshots(db_session)

    assert result["snapshots_deleted"] == 0
    remaining = db_session.query(ConfigSnapshot).filter(ConfigSnapshot.device_id == device.id).count()
    assert remaining == 3


def test_purge_expired_snapshots_deletes_old_beyond_floor(db_session):
    device = _make_device(db_session)
    snaps = [_make_snapshot(db_session, device, version=str(i)) for i in range(5)]
    for s in snaps:
        _backdate(s, days_ago=200, db=db_session)

    with patch("app.core.config.settings.SNAPSHOT_RETENTION_MIN_PER_DEVICE", 2), \
         patch("app.core.config.settings.SNAPSHOT_RETENTION_DAYS", 90):
        result = snapshot_service.purge_expired_snapshots(db_session)

    assert result["snapshots_deleted"] == 3
    remaining = db_session.query(ConfigSnapshot).filter(ConfigSnapshot.device_id == device.id).all()
    assert len(remaining) == 2
    # the 2 most recent (highest seq) survive
    assert {s.version for s in remaining} == {"3", "4"}


def test_purge_expired_snapshots_never_deletes_rollback_referenced_snapshot(db_session):
    device = _make_device(db_session)
    admin = _make_admin(db_session)
    old_snap = _make_snapshot(db_session, device, version="1")
    _backdate(old_snap, days_ago=200, db=db_session)
    for i in range(2, 5):
        s = _make_snapshot(db_session, device, version=str(i))
        _backdate(s, days_ago=200, db=db_session)

    cr = ChangeRequest(
        device_id=device.id, submitted_by=admin.id, priority=ChangePriority.EMERGENCY,
        description="rollback", proposed_config="x", status=ChangeStatus.SUCCESS,
        rollback_snapshot_id=old_snap.id,
    )
    db_session.add(cr)
    db_session.commit()

    with patch("app.core.config.settings.SNAPSHOT_RETENTION_MIN_PER_DEVICE", 1), \
         patch("app.core.config.settings.SNAPSHOT_RETENTION_DAYS", 90):
        snapshot_service.purge_expired_snapshots(db_session)

    survivor_ids = {s.id for s in db_session.query(ConfigSnapshot).filter(ConfigSnapshot.device_id == device.id).all()}
    assert old_snap.id in survivor_ids


def test_purge_expired_snapshots_leaves_recent_snapshots_alone(db_session):
    device = _make_device(db_session)
    _make_snapshot(db_session, device, version="1")  # created just now

    with patch("app.core.config.settings.SNAPSHOT_RETENTION_MIN_PER_DEVICE", 1), \
         patch("app.core.config.settings.SNAPSHOT_RETENTION_DAYS", 90):
        result = snapshot_service.purge_expired_snapshots(db_session)

    assert result["snapshots_deleted"] == 0


def test_retention_status_for_device_reports_counts(db_session):
    device = _make_device(db_session)
    snaps = [_make_snapshot(db_session, device, version=str(i)) for i in range(4)]
    for s in snaps[:3]:
        _backdate(s, days_ago=200, db=db_session)

    with patch("app.core.config.settings.SNAPSHOT_RETENTION_MIN_PER_DEVICE", 1), \
         patch("app.core.config.settings.SNAPSHOT_RETENTION_DAYS", 90):
        status = snapshot_service.retention_status_for_device(db_session, device.id)

    assert status["total_snapshots"] == 4
    assert status["protected_snapshots"] == 1
    assert status["eligible_for_purge"] == 3


# ---------------------------------------------------------------------------
# Rollback preview
# ---------------------------------------------------------------------------
@patch("app.services.rollback_service.deployment_engine.read_running_config", return_value=("interface Gi0/1\n live\n", "ssh"))
def test_preview_rollback_uses_live_config_and_computes_diff(mock_read, db_session):
    device = _make_device(db_session)
    snapshot = _make_snapshot(db_session, device, config="interface Gi0/1\n restored\n", version="1")

    preview = rollback_service.preview_rollback(db_session, device, snapshot)

    assert preview["current_source"] == "live"
    assert preview["identical"] is False
    assert preview["added_lines"] >= 1
    assert preview["removed_lines"] >= 1
    assert preview["warning"] is None
    assert preview["blocked"] is False


@patch("app.services.rollback_service.deployment_engine.read_running_config", return_value=(None, "none"))
def test_preview_rollback_falls_back_to_last_snapshot_with_warning(mock_read, db_session):
    device = _make_device(db_session)
    old_snap = _make_snapshot(db_session, device, config="interface Gi0/1\n old\n", version="1")

    preview = rollback_service.preview_rollback(db_session, device, old_snap)

    assert preview["current_source"] == "last_snapshot"
    assert preview["warning"] is not None
    assert preview["identical"] is True  # only snapshot on file, comparing to itself


@patch("app.services.rollback_service.deployment_engine.read_running_config", return_value=(None, "none"))
def test_preview_rollback_flags_in_flight_change_as_blocked(mock_read, db_session):
    device = _make_device(db_session)
    admin = _make_admin(db_session)
    snapshot = _make_snapshot(db_session, device)

    in_flight = ChangeRequest(
        device_id=device.id, submitted_by=admin.id, priority=ChangePriority.MEDIUM,
        description="unrelated change", proposed_config="interface Gi0/1\n", status=ChangeStatus.DEPLOYING,
    )
    db_session.add(in_flight)
    db_session.commit()

    preview = rollback_service.preview_rollback(db_session, device, snapshot)

    assert preview["blocked"] is True
    assert "already has change request" in preview["blocked_reason"]


@patch("app.services.rollback_service.deployment_engine.read_running_config", return_value=(None, "none"))
def test_preview_rollback_rejects_snapshot_from_another_device(mock_read, db_session):
    device_a = _make_device(db_session, hostname="rtr-a")
    device_b = _make_device(db_session, hostname="rtr-b")
    snapshot = _make_snapshot(db_session, device_a)

    with pytest.raises(rollback_service.RollbackError, match="does not belong to device"):
        rollback_service.preview_rollback(db_session, device_b, snapshot)


def test_preview_rollback_creates_no_change_request(db_session):
    device = _make_device(db_session)
    snapshot = _make_snapshot(db_session, device)

    with patch("app.services.rollback_service.deployment_engine.read_running_config", return_value=(None, "none")):
        rollback_service.preview_rollback(db_session, device, snapshot)

    assert db_session.query(ChangeRequest).count() == 0
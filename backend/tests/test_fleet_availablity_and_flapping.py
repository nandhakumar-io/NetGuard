import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.config_drift import ConfigDrift, DriftBaseline
from app.models.device import Device, DeviceStatus, DeviceVendor
from app.models.device_status_history import DeviceStatusHistory
from app.models.interface_status import InterfaceOperStatus, InterfaceStatus
from app.services import metrics_service


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _device(db, hostname, status, vendor=DeviceVendor.JUNIPER):
    d = Device(hostname=hostname, ip_address="10.0.0.1", vendor=vendor, supports_snmp=True, status=status)
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def test_fleet_availability_excludes_devices_with_no_history(db_session):
    """A device that's never once transitioned status has nothing to time
    -weight -- it should be excluded from the rollup rather than assumed
    100% available."""
    _device(db_session, "never-polled", DeviceStatus.ONLINE)
    summary = metrics_service.fleet_availability_summary(db_session, hours=24)
    assert summary["devices_in_rollup"] == 0
    assert summary["fleet_availability_pct"] is None
    assert summary["fleet_availability_label"] == "n/a"


def test_fleet_availability_time_weights_an_outage(db_session):
    now = datetime.datetime.now(datetime.timezone.utc)
    device = _device(db_session, "core-sw-01", DeviceStatus.OFFLINE)
    db_session.add(DeviceStatusHistory(
        device_id=device.id, status=DeviceStatus.ONLINE, previous_status=None,
        changed_at=now - datetime.timedelta(hours=48),
    ))
    db_session.add(DeviceStatusHistory(
        device_id=device.id, status=DeviceStatus.OFFLINE, previous_status=DeviceStatus.ONLINE,
        changed_at=now - datetime.timedelta(hours=1),
    ))
    db_session.commit()

    summary = metrics_service.fleet_availability_summary(db_session, hours=24)
    assert summary["devices_in_rollup"] == 1
    # 23h available out of 24h window
    assert 95.5 < summary["fleet_availability_pct"] < 96.0
    assert summary["worst_devices"][0]["hostname"] == "core-sw-01"


def test_fleet_availability_degraded_counts_as_available(db_session):
    """DEGRADED (reachable but unhealthy) should count toward uptime, only
    OFFLINE/UNKNOWN should not."""
    now = datetime.datetime.now(datetime.timezone.utc)
    device = _device(db_session, "degraded-box", DeviceStatus.DEGRADED)
    db_session.add(DeviceStatusHistory(
        device_id=device.id, status=DeviceStatus.ONLINE, previous_status=None,
        changed_at=now - datetime.timedelta(hours=48),
    ))
    db_session.add(DeviceStatusHistory(
        device_id=device.id, status=DeviceStatus.DEGRADED, previous_status=DeviceStatus.ONLINE,
        changed_at=now - datetime.timedelta(hours=2),
    ))
    db_session.commit()

    summary = metrics_service.fleet_availability_summary(db_session, hours=24)
    assert summary["fleet_availability_pct"] == 100.0


def test_unstable_devices_combines_and_ranks_all_three_signals(db_session):
    now = datetime.datetime.now(datetime.timezone.utc)
    flappy = _device(db_session, "flappy-mx", DeviceStatus.ONLINE)
    quiet = _device(db_session, "quiet-sw", DeviceStatus.ONLINE)

    for i in range(3):
        db_session.add(DeviceStatusHistory(
            device_id=flappy.id, status=DeviceStatus.OFFLINE, previous_status=DeviceStatus.ONLINE,
            changed_at=now - datetime.timedelta(minutes=10 * i),
        ))
    db_session.add(InterfaceStatus(
        device_id=flappy.id, if_index="1", if_descr="ge-0/0/1",
        status=InterfaceOperStatus.DOWN, previous_status=InterfaceOperStatus.UP,
        changed_at=now - datetime.timedelta(minutes=5),
    ))
    db_session.add(ConfigDrift(
        device_id=flappy.id, baseline=DriftBaseline.PREVIOUS_BACKUP,
        diff_text="x", added_lines=1, removed_lines=0, modified_lines=0,
    ))
    # quiet device: one single reachability blip only
    db_session.add(DeviceStatusHistory(
        device_id=quiet.id, status=DeviceStatus.OFFLINE, previous_status=DeviceStatus.ONLINE,
        changed_at=now - datetime.timedelta(minutes=1),
    ))
    db_session.commit()

    result = metrics_service.unstable_devices(db_session, hours=24, limit=10)
    assert result[0]["hostname"] == "flappy-mx"
    assert result[0]["reachability_flaps"] == 3
    assert result[0]["interface_flaps"] == 1
    assert result[0]["drift_events"] == 1
    assert result[0]["instability_score"] == 3 * 5 + 1 * 2 + 1 * 1
    assert result[1]["hostname"] == "quiet-sw"
    assert result[0]["instability_score"] > result[1]["instability_score"]


def test_unstable_devices_excludes_devices_with_zero_signals(db_session):
    _device(db_session, "boring-box", DeviceStatus.ONLINE)
    result = metrics_service.unstable_devices(db_session, hours=24, limit=10)
    assert result == []

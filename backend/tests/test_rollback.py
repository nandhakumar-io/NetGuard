import os
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.change_request import ChangePriority, ChangeRequest, ChangeStatus
from app.models.device import Device, DeviceVendor
from app.models.snapshot import ConfigSnapshot
from app.models.user import User, UserRole
from app.services import rollback_service, snapshot_service


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


def _make_device(db):
    device = Device(
        hostname="rtr-02",
        ip_address="10.0.0.2",
        vendor=DeviceVendor.CISCO,
        ssh_username="admin",
        ssh_credential_ref="test-rollback-device",
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


def _make_snapshot(db, device, config="interface Gi0/1\n ip address 10.1.1.1 255.255.255.0\n", version="1"):
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


def test_list_snapshots_returns_newest_first(db_session):
    device = _make_device(db_session)
    snap1 = _make_snapshot(db_session, device, version="1")
    snap2 = _make_snapshot(db_session, device, version="2")

    result = rollback_service.list_snapshots(db_session, device.id)

    assert [s.id for s in result] == [snap2.id, snap1.id]


@patch("app.services.rollback_service._live_running_config", new_callable=AsyncMock, return_value=None)
async def test_initiate_rollback_builds_approved_change_request(mock_read, db_session):
    device = _make_device(db_session)
    snapshot = _make_snapshot(db_session, device, config="interface Gi0/1\n ip address 10.1.1.1\n", version="1")
    admin = _make_admin(db_session)

    cr = await rollback_service.initiate_rollback(db_session, device, snapshot, admin, reason="interface flapping")

    assert cr.status == ChangeStatus.APPROVED
    assert cr.is_rollback == "true"
    assert cr.rollback_snapshot_id == snapshot.id
    assert cr.priority == ChangePriority.EMERGENCY
    assert "10.1.1.1" in cr.proposed_config
    assert cr.approved_by == admin.id


@patch("app.services.rollback_service._live_running_config", new_callable=AsyncMock, return_value=None)
async def test_initiate_rollback_rejects_snapshot_from_another_device(mock_read, db_session):
    device_a = _make_device(db_session)
    device_b = Device(
        hostname="rtr-03", ip_address="10.0.0.3", vendor=DeviceVendor.CISCO,
        ssh_username="admin", ssh_credential_ref="test-rollback-device",
    )
    db_session.add(device_b)
    db_session.commit()
    db_session.refresh(device_b)

    snapshot = _make_snapshot(db_session, device_a)
    admin = _make_admin(db_session)

    with pytest.raises(rollback_service.RollbackError, match="does not belong to device"):
        await rollback_service.initiate_rollback(db_session, device_b, snapshot, admin)


@patch("app.services.rollback_service._live_running_config", new_callable=AsyncMock, return_value=None)
async def test_initiate_rollback_rejects_when_device_has_in_flight_change(mock_read, db_session):
    device = _make_device(db_session)
    snapshot = _make_snapshot(db_session, device)
    admin = _make_admin(db_session)

    in_flight = ChangeRequest(
        device_id=device.id, submitted_by=admin.id, priority=ChangePriority.MEDIUM,
        description="unrelated change", proposed_config="interface Gi0/1\n", status=ChangeStatus.DEPLOYING,
    )
    db_session.add(in_flight)
    db_session.commit()

    with pytest.raises(rollback_service.RollbackError, match="already has change request"):
        await rollback_service.initiate_rollback(db_session, device, snapshot, admin)


@patch(
    "app.services.rollback_service._live_running_config",
    new_callable=AsyncMock, return_value="interface Gi0/1\n live\n",
)
async def test_initiate_rollback_uses_live_read_as_current_config_when_available(mock_read, db_session):
    device = _make_device(db_session)
    snapshot = _make_snapshot(db_session, device)
    admin = _make_admin(db_session)

    cr = await rollback_service.initiate_rollback(db_session, device, snapshot, admin)

    assert cr.current_config == "interface Gi0/1\n live\n"


@patch("app.services.rollback_service._live_running_config", new_callable=AsyncMock, return_value=None)
async def test_initiate_rollback_falls_back_to_latest_snapshot_when_live_read_unavailable(mock_read, db_session):
    device = _make_device(db_session)
    snap1 = _make_snapshot(db_session, device, config="interface Gi0/1\n old\n", version="1")
    admin = _make_admin(db_session)

    cr = await rollback_service.initiate_rollback(db_session, device, snap1, admin)

    assert cr.current_config == "interface Gi0/1\n old\n"

"""Coverage for the Discovery-at-Scale changes to the polling sweeps:
per-device interval overrides, due-check skipping, and jittered fan-out.
See app.tasks.run_snmp_poll_sweep_task / run_reachability_sweep_task.
"""
import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.device import Device, DeviceVendor


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _device(**kwargs):
    defaults = dict(hostname="dev", ip_address="10.0.0.1", vendor=DeviceVendor.CISCO, supports_snmp=True)
    defaults.update(kwargs)
    return Device(**defaults)


def test_reachability_sweep_skips_device_not_yet_due(db_session):
    from app import tasks

    now = datetime.datetime.now(datetime.timezone.utc)
    due = _device(hostname="due", last_reachability_poll_at=now - datetime.timedelta(seconds=120))
    not_due = _device(
        hostname="not-due",
        last_reachability_poll_at=now - datetime.timedelta(seconds=5),
        reachability_poll_interval_seconds=300,  # explicit override, longer than default
    )
    db_session.add_all([due, not_due])
    db_session.commit()

    with patch("app.tasks.SessionLocal", return_value=db_session), patch.object(
        db_session, "close", lambda: None
    ), patch("app.tasks.reachability_task.apply_async") as mock_apply:
        enqueued = tasks.run_reachability_sweep_task()

    assert enqueued == 1
    assert mock_apply.call_count == 1
    _, kwargs = mock_apply.call_args
    assert kwargs["args"] == [str(due.id)]
    assert 0 <= kwargs["countdown"] <= 15  # settings.REACHABILITY_POLL_JITTER_SECONDS default


def test_reachability_sweep_never_polled_device_is_always_due(db_session):
    from app import tasks

    device = _device(hostname="never-polled", last_reachability_poll_at=None)
    db_session.add(device)
    db_session.commit()

    with patch("app.tasks.SessionLocal", return_value=db_session), patch.object(
        db_session, "close", lambda: None
    ), patch("app.tasks.reachability_task.apply_async") as mock_apply:
        enqueued = tasks.run_reachability_sweep_task()

    assert enqueued == 1
    mock_apply.assert_called_once()


def test_snmp_sweep_respects_per_device_override(db_session):
    from app import tasks

    now = datetime.datetime.now(datetime.timezone.utc)
    # Fleet default is 60s; this device is overridden to 10s and is due.
    tight = _device(
        hostname="tight",
        last_snmp_poll_at=now - datetime.timedelta(seconds=15),
        snmp_poll_interval_seconds=10,
    )
    # Not SNMP-enabled -- must never be enqueued regardless of timing.
    no_snmp = _device(hostname="no-snmp", supports_snmp=False, last_snmp_poll_at=None)
    db_session.add_all([tight, no_snmp])
    db_session.commit()

    with patch("app.tasks.SessionLocal", return_value=db_session), patch.object(
        db_session, "close", lambda: None
    ), patch("app.tasks.snmp_poll_task.apply_async") as mock_apply:
        enqueued = tasks.run_snmp_poll_sweep_task()

    assert enqueued == 1
    _, kwargs = mock_apply.call_args
    assert kwargs["args"] == [str(tight.id)]


def test_snmp_sweep_jitter_disabled_gives_zero_countdown(db_session):
    from app import tasks
    from app.core.config import settings

    device = _device(hostname="dev1", last_snmp_poll_at=None)
    db_session.add(device)
    db_session.commit()

    with patch("app.tasks.SessionLocal", return_value=db_session), patch.object(
        db_session, "close", lambda: None
    ), patch.object(settings, "SNMP_POLL_JITTER_SECONDS", 0), patch(
        "app.tasks.snmp_poll_task.apply_async"
    ) as mock_apply:
        tasks.run_snmp_poll_sweep_task()

    _, kwargs = mock_apply.call_args
    assert kwargs["countdown"] == 0
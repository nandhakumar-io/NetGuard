"""Tests for app.services.alert_rule_engine -- the evaluator that lets
custom AlertRule rows (previously CRUD-only, never evaluated) actually
fire alerts off live poll data.
"""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.alert import Alert
from app.models.alert_rule import AlertRule
from app.models.device import Device, DeviceVendor
from app.services import alert_rule_engine
from app.services.snmp_service import SnmpMetrics


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _make_device(db, **kw) -> Device:
    device = Device(
        id=uuid.uuid4(), hostname=kw.pop("hostname", "sw1"), ip_address=kw.pop("ip_address", "10.0.0.1"),
        vendor=kw.pop("vendor", DeviceVendor.CISCO), **kw,
    )
    db.add(device)
    db.commit()
    return device


def _make_rule(db, **kw) -> AlertRule:
    rule = AlertRule(
        id=uuid.uuid4(), name=kw.pop("name", "Test rule"), metric=kw.pop("metric", "cpu"),
        operator=kw.pop("operator", "gt"), threshold=kw.pop("threshold", 90.0),
        severity=kw.pop("severity", "warning"), cooldown_seconds=kw.pop("cooldown_seconds", 0),
        enabled=kw.pop("enabled", True), **kw,
    )
    db.add(rule)
    db.commit()
    return rule


def test_breach_raises_custom_rule_alert(db_session):
    device = _make_device(db_session)
    _make_rule(db_session, name="CPU too hot", metric="cpu", operator="gt", threshold=80.0)
    metrics = SnmpMetrics(reachable=True, cpu_utilization_pct=95.0)

    alert_rule_engine.evaluate_rules(db_session, device, metrics)

    alerts = db_session.query(Alert).filter(Alert.device_id == device.id).all()
    assert len(alerts) == 1
    assert alerts[0].category == "Custom Rule: CPU too hot"
    assert not alerts[0].resolved


def test_no_breach_raises_nothing(db_session):
    device = _make_device(db_session)
    _make_rule(db_session, metric="cpu", operator="gt", threshold=80.0)
    metrics = SnmpMetrics(reachable=True, cpu_utilization_pct=10.0)

    alert_rule_engine.evaluate_rules(db_session, device, metrics)

    assert db_session.query(Alert).count() == 0


def test_clearing_condition_auto_resolves(db_session):
    device = _make_device(db_session)
    _make_rule(db_session, name="CPU too hot", metric="cpu", operator="gt", threshold=80.0)

    alert_rule_engine.evaluate_rules(db_session, device, SnmpMetrics(reachable=True, cpu_utilization_pct=95.0))
    alert_rule_engine.evaluate_rules(db_session, device, SnmpMetrics(reachable=True, cpu_utilization_pct=10.0))

    alert = db_session.query(Alert).filter(Alert.device_id == device.id).one()
    assert alert.resolved is True


def test_disabled_rule_is_skipped(db_session):
    device = _make_device(db_session)
    _make_rule(db_session, metric="cpu", operator="gt", threshold=80.0, enabled=False)
    metrics = SnmpMetrics(reachable=True, cpu_utilization_pct=95.0)

    alert_rule_engine.evaluate_rules(db_session, device, metrics)

    assert db_session.query(Alert).count() == 0


def test_scope_filter_excludes_non_matching_device(db_session):
    device = _make_device(db_session, vendor=DeviceVendor.JUNIPER)
    _make_rule(db_session, metric="cpu", operator="gt", threshold=80.0, scope_vendor="cisco")
    metrics = SnmpMetrics(reachable=True, cpu_utilization_pct=95.0)

    alert_rule_engine.evaluate_rules(db_session, device, metrics)

    assert db_session.query(Alert).count() == 0


def test_scope_filter_matches_case_insensitively(db_session):
    device = _make_device(db_session, vendor=DeviceVendor.CISCO)
    _make_rule(db_session, metric="cpu", operator="gt", threshold=80.0, scope_vendor="CISCO")
    metrics = SnmpMetrics(reachable=True, cpu_utilization_pct=95.0)

    alert_rule_engine.evaluate_rules(db_session, device, metrics)

    assert db_session.query(Alert).count() == 1


def test_interface_down_count_metric(db_session):
    device = _make_device(db_session)
    _make_rule(db_session, name="Ports flapping", metric="interface_down_count", operator="gte", threshold=2)
    metrics = SnmpMetrics(
        reachable=True,
        per_interface=[
            {"if_index": 1, "status": "down"},
            {"if_index": 2, "status": "down"},
            {"if_index": 3, "status": "up"},
        ],
    )

    alert_rule_engine.evaluate_rules(db_session, device, metrics)

    alerts = db_session.query(Alert).filter(Alert.device_id == device.id).all()
    assert len(alerts) == 1
    assert alerts[0].category == "Custom Rule: Ports flapping"


def test_fan_failure_metric(db_session):
    device = _make_device(db_session)
    _make_rule(db_session, name="Fan down", metric="fan_failure", operator="eq", threshold=1)
    metrics = SnmpMetrics(reachable=True, fan_status="failed")

    alert_rule_engine.evaluate_rules(db_session, device, metrics)

    assert db_session.query(Alert).filter(Alert.device_id == device.id).count() == 1


def test_missing_metric_value_is_skipped(db_session):
    """A device that doesn't report temperature shouldn't fire a
    temperature rule just because None fails every comparison in a
    surprising way -- it should be skipped outright."""
    device = _make_device(db_session)
    _make_rule(db_session, metric="temperature", operator="gt", threshold=50.0)
    metrics = SnmpMetrics(reachable=True, temperature_celsius=None)

    alert_rule_engine.evaluate_rules(db_session, device, metrics)

    assert db_session.query(Alert).count() == 0


def test_unreachable_device_skips_custom_rules(db_session):
    device = _make_device(db_session)
    _make_rule(db_session, metric="cpu", operator="gt", threshold=1.0)
    metrics = SnmpMetrics(reachable=False, error="timeout")

    alert_rule_engine.evaluate_rules(db_session, device, metrics)

    assert db_session.query(Alert).count() == 0


def test_cooldown_blocks_immediate_refire(db_session):
    device = _make_device(db_session)
    rule = _make_rule(db_session, name="CPU too hot", metric="cpu", operator="gt", threshold=80.0, cooldown_seconds=3600)

    # First breach, then clears.
    alert_rule_engine.evaluate_rules(db_session, device, SnmpMetrics(reachable=True, cpu_utilization_pct=95.0))
    alert_rule_engine.evaluate_rules(db_session, device, SnmpMetrics(reachable=True, cpu_utilization_pct=10.0))
    resolved_alert = db_session.query(Alert).filter(Alert.device_id == device.id).one()
    assert resolved_alert.resolved is True

    # Breaches again immediately -- cooldown should suppress a new alert.
    alert_rule_engine.evaluate_rules(db_session, device, SnmpMetrics(reachable=True, cpu_utilization_pct=95.0))

    active = db_session.query(Alert).filter(Alert.device_id == device.id, Alert.resolved == False).all()
    assert active == []

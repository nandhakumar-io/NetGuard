import uuid
from unittest.mock import MagicMock

from sqlalchemy.orm import Session

from app.models.alert_rule import AlertRule, AlertRuleMetric, AlertRuleOperator
from app.services.alert_rule_engine import evaluate_rules
from app.services.snmp_service import SnmpMetrics


def test_evaluate_temperature():
    db = MagicMock(spec=Session)

    # Mock the rule
    rule = AlertRule(
        id=uuid.uuid4(),
        name="test",
        metric=AlertRuleMetric.TEMPERATURE,
        operator=AlertRuleOperator.GT,
        threshold=40.0,
        severity="critical",
        enabled=True,
        cooldown_seconds=300
    )

    # Mock db.query(...).filter(...).all()
    query_mock = MagicMock()
    filter_mock = MagicMock()
    filter_mock.all.return_value = [rule]
    query_mock.filter.return_value = filter_mock
    db.query.return_value = query_mock

    # Mock device and metrics
    device = MagicMock()
    device.id = uuid.uuid4()
    device.hostname = "test-device"
    device.vendor = None
    device.site = None
    device.device_role = None

    metrics = SnmpMetrics(
        reachable=True,
        temperature_celsius=45.0
    )

    try:
        evaluate_rules(db, device, metrics)
        print("Success! No exceptions.")
    except Exception as e:
        print(f"Exception raised: {e}")
        raise e

if __name__ == "__main__":
    test_evaluate_temperature()

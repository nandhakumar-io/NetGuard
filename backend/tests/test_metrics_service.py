import datetime

from app.models.device_metric import DeviceMetric
from app.services.metrics_service import _compute_interface_utilization
from app.services.snmp_service import SnmpMetrics, compute_health_score


def _metric(octets_total, polled_at):
    row = DeviceMetric()
    row.interface_octets_total = octets_total
    row.polled_at = polled_at
    return row


def test_interface_utilization_none_without_previous_sample():
    metrics = SnmpMetrics(reachable=True, interface_octets_total=1000, interface_speed_bps=1_000_000_000)
    assert _compute_interface_utilization(metrics, previous=None, interval_seconds=60) is None


def test_interface_utilization_computes_delta_over_interval():
    now = datetime.datetime.now(datetime.timezone.utc)
    previous = _metric(octets_total=0, polled_at=now - datetime.timedelta(seconds=60))
    # 60s window, 1 Gbps link, transferred 60,000,000 bytes -> 8,000,000 bps -> 0.8%
    metrics = SnmpMetrics(reachable=True, interface_octets_total=60_000_000, interface_speed_bps=1_000_000_000)
    pct = _compute_interface_utilization(metrics, previous, interval_seconds=60)
    assert pct == 0.8


def test_interface_utilization_none_on_counter_reset():
    now = datetime.datetime.now(datetime.timezone.utc)
    previous = _metric(octets_total=500_000, polled_at=now - datetime.timedelta(seconds=60))
    metrics = SnmpMetrics(reachable=True, interface_octets_total=100, interface_speed_bps=1_000_000_000)
    assert _compute_interface_utilization(metrics, previous, interval_seconds=60) is None


def test_health_score_unreachable_is_red():
    score, color = compute_health_score(SnmpMetrics(reachable=False))
    assert score == 0
    assert color == "red"


def test_health_score_excludes_missing_readings():
    metrics = SnmpMetrics(reachable=True, cpu_utilization_pct=10.0)
    score, color = compute_health_score(metrics)
    assert score == 90
    assert color == "green"
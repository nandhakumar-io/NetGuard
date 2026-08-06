"""Coverage for the pure-logic parts of app.services.path_trace_service:
hop-status classification and real-traceroute output parsing. The
topology-BFS fallback and full run_trace() persistence path need a DB
session (exercised via the API layer in integration testing, same as
the rest of this codebase's DB-touching services), so this file sticks
to what can be verified offline.
"""
from unittest.mock import patch

from app.models.path_trace import HopStatus
from app.services import path_trace_service as pts


def test_hop_status_ok_for_fast_clean_hop():
    assert pts._hop_status_for(rtt_ms=12.0, loss_pct=0.0) == HopStatus.OK


def test_hop_status_timeout_when_no_rtt():
    assert pts._hop_status_for(rtt_ms=None, loss_pct=100.0) == HopStatus.TIMEOUT


def test_hop_status_degraded_for_slow_hop():
    assert pts._hop_status_for(rtt_ms=250.0, loss_pct=0.0) == HopStatus.DEGRADED


def test_hop_status_degraded_for_partial_loss_even_if_fast():
    assert pts._hop_status_for(rtt_ms=20.0, loss_pct=33.0) == HopStatus.DEGRADED


def test_resolve_target_ip_passes_through_a_literal_ip():
    assert pts._resolve_target_ip("10.0.0.5") == "10.0.0.5"


def test_resolve_target_ip_returns_none_for_unresolvable_hostname():
    with patch("socket.gethostbyname", side_effect=OSError):
        assert pts._resolve_target_ip("this-host-does-not-exist.invalid") is None


_LINUX_TRACEROUTE_OUTPUT = """traceroute to 8.8.8.8 (8.8.8.8), 30 hops max, 60 byte packets
 1  10.0.0.1  1.204 ms  1.150 ms  1.098 ms
 2  10.0.1.1  3.501 ms  3.450 ms  3.402 ms
 3  * * *
 4  8.8.8.8  14.203 ms  14.150 ms  14.099 ms
"""


def test_run_traceroute_parses_real_output_including_a_silent_hop(monkeypatch):
    monkeypatch.setattr(pts.shutil, "which", lambda name: "/usr/bin/traceroute" if name == "traceroute" else None)

    fake_result = type("R", (), {"stdout": _LINUX_TRACEROUTE_OUTPUT, "returncode": 0})()
    with patch.object(pts.subprocess, "run", return_value=fake_result):
        hops = pts._run_traceroute("8.8.8.8")

    assert hops is not None
    assert [h["hop_index"] for h in hops] == [1, 2, 3, 4]

    assert hops[0]["ip_address"] == "10.0.0.1"
    assert hops[0]["rtt_ms"] is not None and 1.0 <= hops[0]["rtt_ms"] <= 1.5
    assert hops[0]["loss_pct"] == 0.0

    # hop 3 is a fully silent hop ("* * *") -- must be captured, not
    # dropped, with total loss and no RTT.
    assert hops[2]["ip_address"] is None
    assert hops[2]["rtt_ms"] is None
    assert hops[2]["loss_pct"] == 100.0

    assert hops[3]["ip_address"] == "8.8.8.8"


def test_run_traceroute_returns_none_when_binary_missing(monkeypatch):
    monkeypatch.setattr(pts.shutil, "which", lambda name: None)
    assert pts._run_traceroute("8.8.8.8") is None

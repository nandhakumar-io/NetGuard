"""Coverage for app.services.health_monitor (SRS 6.9 Real-Time Health
Monitoring). Every check that touches the network (ping, NAPALM, DNS,
HTTP) is exercised against mocks so the suite runs offline and fast, but
each test still asserts the real parsing/pass-fail logic in
health_monitor.py rather than just "it didn't raise".
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services import health_monitor as hm


# ---------------------------------------------------------------------
# infrastructure: ping / packet loss / latency
# ---------------------------------------------------------------------
LINUX_PING_OK = """PING 10.0.0.1 (10.0.0.1): 56 data bytes
--- 10.0.0.1 ping statistics ---
4 packets transmitted, 4 received, 0% packet loss, time 3003ms
rtt min/avg/max/mdev = 0.021/0.045/0.089/0.012 ms
"""

LINUX_PING_LOSSY = """PING 10.0.0.2 (10.0.0.2): 56 data bytes
--- 10.0.0.2 ping statistics ---
4 packets transmitted, 1 received, 75% packet loss, time 3010ms
rtt min/avg/max/mdev = 210.021/250.045/300.089/12.012 ms
"""

LINUX_PING_TOTAL_LOSS = """PING 10.0.0.3 (10.0.0.3): 56 data bytes
--- 10.0.0.3 ping statistics ---
4 packets transmitted, 0 received, 100% packet loss, time 3010ms
"""


def _fake_completed(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def test_check_ping_passes_on_success():
    with patch("subprocess.run", return_value=_fake_completed(stdout=LINUX_PING_OK, returncode=0)):
        outcome = hm.check_ping("10.0.0.1")
    assert outcome.category == "infrastructure"
    assert outcome.check_name == "ping"
    assert outcome.passed is True


def test_check_ping_fails_on_nonzero_exit():
    with patch("subprocess.run", return_value=_fake_completed(stderr="Destination unreachable", returncode=1)):
        outcome = hm.check_ping("10.0.0.99")
    assert outcome.passed is False


def test_check_ping_handles_subprocess_error():
    with patch("subprocess.run", side_effect=OSError("ping binary not found")):
        outcome = hm.check_ping("10.0.0.1")
    assert outcome.passed is False
    assert "ping binary not found" in outcome.detail


@pytest.mark.parametrize(
    "stdout,expected_loss,expected_latency",
    [
        (LINUX_PING_OK, 0.0, 0.045),
        (LINUX_PING_LOSSY, 75.0, 250.045),
        (LINUX_PING_TOTAL_LOSS, 100.0, None),
    ],
)
def test_parse_ping_stats(stdout, expected_loss, expected_latency):
    loss, latency = hm._parse_ping_stats(stdout)
    assert loss == expected_loss
    assert latency == expected_latency


def test_packet_loss_and_latency_passes_within_thresholds():
    with patch("subprocess.run", return_value=_fake_completed(stdout=LINUX_PING_OK, returncode=0)):
        outcome = hm.check_packet_loss_and_latency("10.0.0.1")
    assert outcome.passed is True
    assert "loss=0.0%" in outcome.detail


def test_packet_loss_and_latency_fails_over_loss_threshold():
    with patch("subprocess.run", return_value=_fake_completed(stdout=LINUX_PING_LOSSY, returncode=1)):
        outcome = hm.check_packet_loss_and_latency("10.0.0.2", max_loss_pct=10.0, max_latency_ms=200.0)
    assert outcome.passed is False
    assert "exceeds 10.0% threshold" in outcome.detail
    assert "exceeds 200.0ms threshold" in outcome.detail


def test_packet_loss_and_latency_unparseable_output_fails():
    with patch("subprocess.run", return_value=_fake_completed(stdout="garbage", returncode=1)):
        outcome = hm.check_packet_loss_and_latency("10.0.0.5")
    assert outcome.passed is False
    assert "Could not parse" in outcome.detail


# ---------------------------------------------------------------------
# routing: BGP / OSPF via NAPALM
# ---------------------------------------------------------------------
def _mock_napalm_driver(getter_return):
    """Builds a fake `napalm.get_network_driver(...)(...)` chain whose
    instance's arbitrary getter method returns `getter_return`."""
    device = MagicMock()
    device.open.return_value = None
    device.close.return_value = None
    device.get_bgp_neighbors.return_value = getter_return
    device.get_ospf_neighbors.return_value = getter_return
    device.get_facts.return_value = getter_return
    driver_factory = MagicMock(return_value=device)
    fake_napalm_module = SimpleNamespace(get_network_driver=MagicMock(return_value=driver_factory))
    return fake_napalm_module, device


def test_bgp_neighbors_all_up():
    bgp_data = {"global": {"peers": {"10.1.1.1": {"is_up": True}, "10.1.1.2": {"is_up": True}}}}
    fake_napalm, _ = _mock_napalm_driver(bgp_data)
    with patch.dict("sys.modules", {"napalm": fake_napalm}):
        outcome = hm.check_bgp_neighbors("cisco_ios", "10.0.0.1", "admin", "pw")
    assert outcome.category == "routing"
    assert outcome.check_name == "bgp_neighbor"
    assert outcome.passed is True
    assert "2/2" in outcome.detail


def test_bgp_neighbors_partial_down_fails():
    bgp_data = {"global": {"peers": {"10.1.1.1": {"is_up": True}, "10.1.1.2": {"is_up": False}}}}
    fake_napalm, _ = _mock_napalm_driver(bgp_data)
    with patch.dict("sys.modules", {"napalm": fake_napalm}):
        outcome = hm.check_bgp_neighbors("cisco_ios", "10.0.0.1", "admin", "pw")
    assert outcome.passed is False
    assert "1/2" in outcome.detail


def test_bgp_neighbors_not_applicable_for_unsupported_platform():
    outcome = hm.check_bgp_neighbors("linux", "10.0.0.1", "admin", "pw")
    assert outcome.passed is True
    assert "Not applicable" in outcome.detail


def test_bgp_neighbors_none_configured_passes():
    fake_napalm, _ = _mock_napalm_driver({"global": {"peers": {}}})
    with patch.dict("sys.modules", {"napalm": fake_napalm}):
        outcome = hm.check_bgp_neighbors("cisco_ios", "10.0.0.1", "admin", "pw")
    assert outcome.passed is True
    assert "No BGP neighbors" in outcome.detail


def test_bgp_neighbors_connection_error_fails():
    fake_napalm = SimpleNamespace(
        get_network_driver=MagicMock(side_effect=ConnectionError("auth failed"))
    )
    with patch.dict("sys.modules", {"napalm": fake_napalm}):
        outcome = hm.check_bgp_neighbors("cisco_ios", "10.0.0.1", "admin", "wrongpw")
    assert outcome.passed is False
    assert "BGP check failed" in outcome.detail


def test_ospf_neighbors_all_full():
    ospf_data = {"global": {"neighbors": {"Gi0/1": [{"state": "FULL"}, {"state": "Full"}]}}}
    fake_napalm, _ = _mock_napalm_driver(ospf_data)
    with patch.dict("sys.modules", {"napalm": fake_napalm}):
        outcome = hm.check_ospf_neighbors("cisco_ios", "10.0.0.1", "admin", "pw")
    assert outcome.category == "routing"
    assert outcome.check_name == "ospf_neighbor"
    assert outcome.passed is True
    assert "2/2" in outcome.detail


def test_ospf_neighbors_not_full_fails():
    ospf_data = {"global": {"neighbors": {"Gi0/1": [{"state": "2WAY"}]}}}
    fake_napalm, _ = _mock_napalm_driver(ospf_data)
    with patch.dict("sys.modules", {"napalm": fake_napalm}):
        outcome = hm.check_ospf_neighbors("cisco_ios", "10.0.0.1", "admin", "pw")
    assert outcome.passed is False
    assert "0/1" in outcome.detail


def test_ospf_neighbors_not_applicable_for_unsupported_platform():
    outcome = hm.check_ospf_neighbors("linux", "10.0.0.1", "admin", "pw")
    assert outcome.passed is True
    assert "Not applicable" in outcome.detail


# ---------------------------------------------------------------------
# services: DNS / DHCP / HTTP / VPN
# ---------------------------------------------------------------------
def test_dns_forward_lookup_passes():
    with patch("socket.gethostbyname", return_value="10.0.0.1"):
        outcome = hm.check_dns("switch1.lab.local")
    assert outcome.category == "services"
    assert outcome.check_name == "dns"
    assert outcome.passed is True
    assert "resolved to 10.0.0.1" in outcome.detail


def test_dns_reverse_lookup_for_ip_input():
    with patch("socket.gethostbyaddr", return_value=("switch1.lab.local", [], ["10.0.0.1"])):
        outcome = hm.check_dns("10.0.0.1")
    assert outcome.passed is True
    assert "switch1.lab.local" in outcome.detail


def test_dns_missing_ptr_record_not_fatal():
    import socket as socket_module

    with patch("socket.gethostbyaddr", side_effect=socket_module.herror("no PTR")):
        outcome = hm.check_dns("10.0.0.1")
    assert outcome.passed is True
    assert "No PTR record" in outcome.detail


def test_dns_resolution_failure():
    with patch("socket.gethostbyname", side_effect=OSError("NXDOMAIN")):
        outcome = hm.check_dns("doesnotexist.lab.local")
    assert outcome.passed is False
    assert "DNS resolution failed" in outcome.detail


def test_dhcp_valid_lease_passes():
    fake_napalm, _ = _mock_napalm_driver({"hostname": "sw1"})
    with patch.dict("sys.modules", {"napalm": fake_napalm}):
        outcome = hm.check_dhcp("cisco_ios", "10.0.0.1", "admin", "pw")
    assert outcome.category == "services"
    assert outcome.check_name == "dhcp"
    assert outcome.passed is True


def test_dhcp_link_local_address_fails():
    fake_napalm, _ = _mock_napalm_driver({"hostname": "sw1"})
    with patch.dict("sys.modules", {"napalm": fake_napalm}):
        outcome = hm.check_dhcp("cisco_ios", "169.254.1.5", "admin", "pw")
    assert outcome.passed is False
    assert "link-local" in outcome.detail


def test_dhcp_not_applicable_for_unsupported_platform():
    outcome = hm.check_dhcp("linux", "10.0.0.1", "admin", "pw")
    assert outcome.passed is True
    assert "Not applicable" in outcome.detail


def test_http_passes_on_response():
    fake_response = SimpleNamespace(status_code=200)
    with patch("httpx.get", return_value=fake_response):
        outcome = hm.check_http("10.0.0.1")
    assert outcome.category == "services"
    assert outcome.check_name == "http"
    assert outcome.passed is True
    assert "200" in outcome.detail


def test_http_fails_when_unreachable():
    import httpx as httpx_module

    with patch("httpx.get", side_effect=httpx_module.ConnectError("connection refused")):
        outcome = hm.check_http("10.0.0.99")
    assert outcome.passed is False
    assert "No HTTP(S) response" in outcome.detail


def test_vpn_all_tunnels_up():
    device = MagicMock()
    device.get_ipsec_ike_sas.return_value = {
        "tunnel1": {"state": "up"},
        "tunnel2": {"state": "established"},
    }
    driver_factory = MagicMock(return_value=device)
    fake_napalm = SimpleNamespace(get_network_driver=MagicMock(return_value=driver_factory))
    with patch.dict("sys.modules", {"napalm": fake_napalm}):
        outcome = hm.check_vpn("cisco_ios", "10.0.0.1", "admin", "pw")
    assert outcome.category == "services"
    assert outcome.check_name == "vpn"
    assert outcome.passed is True
    assert "2/2" in outcome.detail


def test_vpn_tunnel_down_fails():
    device = MagicMock()
    device.get_ipsec_ike_sas.return_value = {"tunnel1": {"state": "down"}}
    driver_factory = MagicMock(return_value=device)
    fake_napalm = SimpleNamespace(get_network_driver=MagicMock(return_value=driver_factory))
    with patch.dict("sys.modules", {"napalm": fake_napalm}):
        outcome = hm.check_vpn("cisco_ios", "10.0.0.1", "admin", "pw")
    assert outcome.passed is False
    assert "0/1" in outcome.detail


def test_vpn_none_configured_passes():
    device = MagicMock()
    device.get_ipsec_ike_sas.return_value = {}
    driver_factory = MagicMock(return_value=device)
    fake_napalm = SimpleNamespace(get_network_driver=MagicMock(return_value=driver_factory))
    with patch.dict("sys.modules", {"napalm": fake_napalm}):
        outcome = hm.check_vpn("cisco_ios", "10.0.0.1", "admin", "pw")
    assert outcome.passed is True
    assert "No VPN tunnels" in outcome.detail


def test_vpn_not_applicable_for_unsupported_platform():
    outcome = hm.check_vpn("linux", "10.0.0.1", "admin", "pw")
    assert outcome.passed is True
    assert "Not applicable" in outcome.detail


# ---------------------------------------------------------------------
# suite orchestration
# ---------------------------------------------------------------------
def test_run_health_suite_covers_all_categories():
    """SRS 6.9 requires infrastructure, routing, and services categories
    to all be represented in a single suite run."""
    with patch.object(hm, "check_ping") as m_ping, \
         patch.object(hm, "check_packet_loss_and_latency") as m_loss, \
         patch.object(hm, "check_bgp_neighbors") as m_bgp, \
         patch.object(hm, "check_ospf_neighbors") as m_ospf, \
         patch.object(hm, "check_dns") as m_dns, \
         patch.object(hm, "check_dhcp") as m_dhcp, \
         patch.object(hm, "check_http") as m_http, \
         patch.object(hm, "check_vpn") as m_vpn:
        for mock, category, name in [
            (m_ping, "infrastructure", "ping"),
            (m_loss, "infrastructure", "packet_loss_latency"),
            (m_bgp, "routing", "bgp_neighbor"),
            (m_ospf, "routing", "ospf_neighbor"),
            (m_dns, "services", "dns"),
            (m_dhcp, "services", "dhcp"),
            (m_http, "services", "http"),
            (m_vpn, "services", "vpn"),
        ]:
            mock.return_value = hm.CheckOutcome(category, name, True, "ok")

        results = hm.run_health_suite("10.0.0.1", netmiko_type="cisco_ios", username="admin", password="pw")

    categories = {r.category for r in results}
    assert categories == {"infrastructure", "routing", "services"}
    check_names = {r.check_name for r in results}
    assert check_names == {
        "ping", "packet_loss_latency", "bgp_neighbor", "ospf_neighbor", "dns", "dhcp", "http", "vpn",
    }
    assert len(results) == 8


def test_suite_passed_true_when_all_checks_pass():
    results = [hm.CheckOutcome("infrastructure", "ping", True, "ok")] * 3
    assert hm.suite_passed(results) is True


def test_suite_passed_false_when_any_check_fails():
    results = [
        hm.CheckOutcome("infrastructure", "ping", True, "ok"),
        hm.CheckOutcome("routing", "bgp_neighbor", False, "down"),
    ]
    assert hm.suite_passed(results) is False


def test_run_health_suite_isolates_check_failures():
    """One check raising shouldn't be possible to reach run_health_suite
    unhandled -- every check function catches its own exceptions -- but
    this asserts the suite still returns a full result set even when the
    underlying dependency (NAPALM) throws mid-suite."""
    fake_napalm = SimpleNamespace(get_network_driver=MagicMock(side_effect=RuntimeError("napalm not installed")))
    with patch("subprocess.run", return_value=_fake_completed(stdout=LINUX_PING_OK, returncode=0)), \
         patch("socket.gethostbyname", return_value="10.0.0.1"), \
         patch("httpx.get", return_value=SimpleNamespace(status_code=200)), \
         patch.dict("sys.modules", {"napalm": fake_napalm}):
        results = hm.run_health_suite("10.0.0.1", netmiko_type="cisco_ios", username="admin", password="pw")

    assert len(results) == 8
    bgp_result = next(r for r in results if r.check_name == "bgp_neighbor")
    assert bgp_result.passed is False
    assert "napalm not installed" in bgp_result.detail
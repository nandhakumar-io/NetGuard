"""Proves the health_monitor gap identified in Phase 1 is actually closed:
when DEVICE_GATEWAY_ENABLED, pipeline_service.run_deployment_for_device's
post-deploy monitoring window must not call credential_service.get_ssh_
password (i.e. must never decrypt/hold the device's SSH credential in the
worker process), and the four checks that used to need that credential
(bgp_neighbor, ospf_neighbor, dhcp, vpn) must be answered via
device_job_service.submit_job_sync against the Device Gateway instead.

Also exercises the pure from_raw interpreters directly, since those now
carry the only copy of the pass/fail scoring logic (shared between the
legacy in-process path and the Gateway-backed path).
"""
from unittest.mock import patch

from app.schemas.device_job import DeviceJobResult, DeviceOperation
from app.services import health_monitor, pipeline_service


class _FakeDevice:
    def __init__(self):
        self.tenant_id = "tenant-1"
        self.id = "device-1"
        self.ip_address = "10.0.0.1"
        self.ssh_username = "admin"


def test_gateway_check_overrides_never_touch_credential_service():
    device = _FakeDevice()

    fake_bgp = DeviceJobResult(
        job_id="j1", success=True,
        output='{"global": {"peers": {"10.0.0.2": {"is_up": true}}}}',
        executed_at="2026-01-01T00:00:00+00:00",
    )

    with patch("app.services.credential_service.get_ssh_password") as mock_get_password, \
         patch.object(pipeline_service.device_job_service, "submit_job_sync", return_value=fake_bgp) as mock_submit:
        overrides = pipeline_service._gateway_check_overrides(device, actor_email="alice@example.com")
        outcome = overrides["bgp_neighbor"]()

    mock_get_password.assert_not_called()
    mock_submit.assert_called_once()
    _, kwargs = mock_submit.call_args
    assert kwargs["operation"] == DeviceOperation.GET_BGP_NEIGHBORS
    assert kwargs["tenant_id"] == "tenant-1"
    assert kwargs["device_id"] == "device-1"
    assert outcome.passed is True
    assert outcome.category == "routing"
    assert outcome.check_name == "bgp_neighbor"


def test_gateway_check_overrides_covers_all_four_credentialed_checks():
    device = _FakeDevice()
    overrides = pipeline_service._gateway_check_overrides(device, actor_email="alice@example.com")
    assert set(overrides.keys()) == {"bgp_neighbor", "ospf_neighbor", "dhcp", "vpn"}


def test_gateway_check_timeout_fails_the_check_not_the_whole_suite():
    device = _FakeDevice()
    with patch.object(
        pipeline_service.device_job_service, "submit_job_sync",
        side_effect=pipeline_service.device_job_service.DeviceJobTimeoutError("no response"),
    ):
        overrides = pipeline_service._gateway_check_overrides(device, actor_email="alice@example.com")
        outcome = overrides["vpn"]()
    assert outcome.passed is False
    assert "timeout" in outcome.detail.lower()


# --- pure interpreter behavior (shared by legacy + Gateway paths) --------

def test_check_bgp_neighbors_from_raw_none_is_not_applicable():
    outcome = health_monitor.check_bgp_neighbors_from_raw(None)
    assert outcome.passed is True
    assert "not applicable" in outcome.detail.lower()


def test_check_bgp_neighbors_from_raw_scores_up_vs_total():
    raw = {"global": {"peers": {"10.0.0.2": {"is_up": True}, "10.0.0.3": {"is_up": False}}}}
    outcome = health_monitor.check_bgp_neighbors_from_raw(raw)
    assert outcome.passed is False
    assert outcome.detail == "1/2 BGP neighbor(s) up"


def test_check_dhcp_from_raw_flags_link_local_address():
    outcome = health_monitor.check_dhcp_from_raw({"hostname": "r1"}, "169.254.1.1")
    assert outcome.passed is False


def test_check_vpn_from_raw_no_tunnels_passes_trivially():
    outcome = health_monitor.check_vpn_from_raw({})
    assert outcome.passed is True
    assert "no vpn tunnels" in outcome.detail.lower()

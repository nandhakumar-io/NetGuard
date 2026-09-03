import httpx
import pytest
import respx

from app.services.opa_service import OpaDecision, OpaService

OPA_URL = "http://opa-test:8181"
POLICY_PATH = "/v1/data/netguard"


def _service(**overrides) -> OpaService:
    kwargs = dict(base_url=OPA_URL, policy_path=POLICY_PATH, timeout_seconds=1.0, fail_closed=True, enabled=True)
    kwargs.update(overrides)
    return OpaService(**kwargs)


def _opa_response(*, deny=False, review=False, violations=None, warnings=None, matched=None, version="v1"):
    return {
        "result": {
            "deny": deny,
            "review": review,
            "violations": violations or [],
            "warnings": warnings or [],
            "matched_policies": matched or [],
            "policy_version": version,
        }
    }


CHANGE = {"id": "cr-1", "description": "test", "priority": "medium"}
DEVICE = {"id": "dev-1", "hostname": "rtr-01", "vendor": "cisco", "platform": "ios", "role": "access"}


@pytest.mark.asyncio
@respx.mock
async def test_safe_config_allows():
    respx.post(f"{OPA_URL}{POLICY_PATH}").mock(return_value=httpx.Response(200, json=_opa_response()))
    result = await _service().evaluate_change(DEVICE, "current", "interface Gi0/1\n", CHANGE)
    assert result.decision == OpaDecision.ALLOW
    assert result.passed is True


@pytest.mark.asyncio
@respx.mock
async def test_telnet_denies():
    respx.post(f"{OPA_URL}{POLICY_PATH}").mock(
        return_value=httpx.Response(
            200,
            json=_opa_response(
                deny=True,
                violations=[
                    {
                        "policy": "network_security.no_telnet",
                        "severity": "critical",
                        "message": "Telnet must not be enabled.",
                    }
                ],
                matched=["network_security.no_telnet"],
            ),
        )
    )
    result = await _service().evaluate_change(DEVICE, "current", "transport input telnet\n", CHANGE)
    assert result.decision == OpaDecision.DENY
    assert result.passed is False
    assert result.violations[0].policy == "network_security.no_telnet"


@pytest.mark.asyncio
@respx.mock
async def test_guest_to_management_denies():
    respx.post(f"{OPA_URL}{POLICY_PATH}").mock(
        return_value=httpx.Response(
            200,
            json=_opa_response(
                deny=True,
                violations=[
                    {
                        "policy": "segmentation.guest_not_to_management",
                        "severity": "critical",
                        "message": "Guest VLAN reaches management VLAN.",
                    }
                ],
            ),
        )
    )
    result = await _service().evaluate_change(DEVICE, None, "permit ip vlan50 any vlan10 any\n", CHANGE)
    assert result.decision == OpaDecision.DENY


@pytest.mark.asyncio
@respx.mock
async def test_unauthorized_default_route_denies():
    respx.post(f"{OPA_URL}{POLICY_PATH}").mock(
        return_value=httpx.Response(
            200,
            json=_opa_response(
                deny=True,
                violations=[
                    {
                        "policy": "routing.unauthorized_default_route",
                        "severity": "high",
                        "message": "Unauthorized default route next-hop.",
                    }
                ],
            ),
        )
    )
    result = await _service().evaluate_change(DEVICE, None, "ip route 0.0.0.0 0.0.0.0 10.9.9.9\n", CHANGE)
    assert result.decision == OpaDecision.DENY


@pytest.mark.asyncio
@respx.mock
async def test_critical_change_reviews():
    respx.post(f"{OPA_URL}{POLICY_PATH}").mock(
        return_value=httpx.Response(
            200,
            json=_opa_response(
                review=True,
                violations=[
                    {
                        "policy": "change_management.high_blast_radius",
                        "severity": "medium",
                        "message": "High blast radius change.",
                    }
                ],
            ),
        )
    )
    result = await _service().evaluate_change(DEVICE, None, "interface Gi0/1\n", CHANGE)
    assert result.decision == OpaDecision.REVIEW
    assert result.passed is True  # REVIEW never blocks by itself at the OPA layer


@pytest.mark.asyncio
@respx.mock
async def test_opa_unreachable_fail_closed_blocks():
    respx.post(f"{OPA_URL}{POLICY_PATH}").mock(side_effect=httpx.ConnectError("connection refused"))
    result = await _service(fail_closed=True).evaluate_change(DEVICE, None, "interface Gi0/1\n", CHANGE)
    assert result.decision == OpaDecision.DENY
    assert result.passed is False
    assert result.error is not None


@pytest.mark.asyncio
@respx.mock
async def test_opa_unreachable_fail_open_proceeds():
    respx.post(f"{OPA_URL}{POLICY_PATH}").mock(side_effect=httpx.ConnectError("connection refused"))
    result = await _service(fail_closed=False).evaluate_change(DEVICE, None, "interface Gi0/1\n", CHANGE)
    assert result.decision == OpaDecision.UNAVAILABLE
    assert result.passed is True


@pytest.mark.asyncio
async def test_opa_disabled_skips_call():
    service = _service(enabled=False)
    result = await service.evaluate_change(DEVICE, None, "interface Gi0/1\n", CHANGE)
    assert result.decision == OpaDecision.ALLOW
    assert result.passed is True


def test_build_opa_input_never_includes_credentials():
    from app.services.opa_service import build_opa_input

    device = dict(DEVICE, ssh_password="supersecret", api_token="tok-123")
    user_context = {"id": "u1", "roles": ["network_admin"], "session_token": "leaked-if-included"}
    payload = build_opa_input(
        device=device, current_config=None, proposed_config="x", change_request=CHANGE, user_context=user_context
    )
    flat = str(payload)
    assert "supersecret" not in flat
    assert "tok-123" not in flat
    assert "leaked-if-included" not in flat

"""Unit tests for app.services.authz_service (OPA API-level authorization).

All tests mock the OPA HTTP endpoint via respx so no real OPA process is needed.
We test the four scenarios described in the implementation plan:

  1. network_admin user in own tenant   → allowed
  2. network_engineer doing device:write → denied (only config/terminal actions)
  3. MSP staff (is_msp_staff=True)       → always allowed
  4. OPA unreachable + fail_closed=False → allowed (fail-open default)
  5. OPA unreachable + fail_closed=True  → denied
  6. OPA returns undefined result         → denied (safe default)
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_user(
    *,
    role: str = "network_admin",
    tenant_id: str | None = None,
    is_msp_staff: bool = False,
    extra_roles: str = "",
) -> MagicMock:
    """Build a minimal User-like mock for testing."""
    tenant_uuid = tenant_id or str(uuid.uuid4())
    user = MagicMock()
    user.id = uuid.uuid4()
    user.role = SimpleNamespace(value=role)
    user.tenant_id = uuid.UUID(tenant_uuid)
    user.is_msp_staff = is_msp_staff
    user.extra_roles = extra_roles
    return user


def _make_service(*, fail_closed: bool = False, enabled: bool = True):
    """Instantiate AuthzService with explicit overrides (avoids reading settings)."""
    from app.services.authz_service import AuthzService

    return AuthzService(
        base_url="http://opa-test:8181",
        policy_path="/v1/data/netguard/authz/allow",
        timeout_seconds=1.0,
        fail_closed=fail_closed,
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self) -> dict:
        return self._body


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_opa_disabled_always_allows():
    """When OPA_ENABLED=False the service returns True without any HTTP call."""
    svc = _make_service(enabled=False)
    user = _make_user(role="noc_engineer")
    # Would be denied by policy if OPA were enabled
    result = await svc.check(user=user, action="device:write", resource={})
    assert result is True


@pytest.mark.asyncio
async def test_opa_allows_network_admin():
    """OPA returning {result: true} -> check() returns True."""
    svc = _make_service()
    user = _make_user(role="network_admin")

    async def _post(*args, **kwargs):
        return _FakeResponse({"result": True})

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=_post)
        mock_client_cls.return_value = mock_client

        result = await svc.check(user=user, action="device:write", resource={"device_tenant_id": str(user.tenant_id)})

    assert result is True


@pytest.mark.asyncio
async def test_opa_denies_engineer_device_write():
    """OPA returning {result: false} -> check() returns False."""
    svc = _make_service()
    user = _make_user(role="network_engineer")

    async def _post(*args, **kwargs):
        return _FakeResponse({"result": False})

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=_post)
        mock_client_cls.return_value = mock_client

        result = await svc.check(user=user, action="device:write", resource={"device_tenant_id": str(user.tenant_id)})

    assert result is False


@pytest.mark.asyncio
async def test_opa_unreachable_fail_open():
    """OPA down + fail_closed=False → allow (fail-open)."""
    import httpx

    svc = _make_service(fail_closed=False)
    user = _make_user(role="network_admin")

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client_cls.return_value = mock_client

        result = await svc.check(user=user, action="device:write", resource={})

    assert result is True


@pytest.mark.asyncio
async def test_opa_unreachable_fail_closed():
    """OPA down + fail_closed=True → deny."""
    import httpx

    svc = _make_service(fail_closed=True)
    user = _make_user(role="network_admin")

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client_cls.return_value = mock_client

        result = await svc.check(user=user, action="device:write", resource={})

    assert result is False


@pytest.mark.asyncio
async def test_opa_undefined_result_denies():
    """OPA returning no 'result' key (undefined rule) → deny."""
    svc = _make_service()
    user = _make_user(role="network_admin")

    async def _post(*args, **kwargs):
        return _FakeResponse({})  # no "result" key = undefined

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=_post)
        mock_client_cls.return_value = mock_client

        result = await svc.check(user=user, action="some:new:action", resource={})

    assert result is False


@pytest.mark.asyncio
async def test_msp_staff_allowed_across_tenants():
    """MSP staff (is_msp_staff=True) should be allowed cross-tenant by OPA."""
    svc = _make_service()
    user = _make_user(role="network_admin", is_msp_staff=True)

    async def _post(*args, **kwargs):
        # MSP staff → OPA returns true
        return _FakeResponse({"result": True})

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=_post)
        mock_client_cls.return_value = mock_client

        result = await svc.check(
            user=user,
            action="device:write",
            resource={"device_tenant_id": str(uuid.uuid4())},  # different tenant
        )

    assert result is True

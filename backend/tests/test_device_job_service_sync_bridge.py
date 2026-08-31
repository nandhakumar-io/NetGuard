"""Proves device_job_service.submit_job_sync() actually works.

Every DEVICE_GATEWAY_ENABLED call site inside pipeline_service.py (get
running config before deploy, deploy itself, rollback) calls
`device_job_service.submit_job_sync(...)` from plain `def` functions
driven by Celery tasks. submit_job_sync() did not exist until this pass
-- only `async def submit_job` did -- so every one of those call sites
would raise AttributeError the instant it executed with the Gateway
migration turned on. This test exists so that regression can't silently
reappear.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.schemas.device_job import DeviceJobResult, DeviceOperation
from app.services import device_job_service


def test_submit_job_sync_bridges_to_async_submit_job():
    fake_result = DeviceJobResult(
        job_id="job-1",
        success=True,
        output="hostname R1",
        executed_at="2026-01-01T00:00:00+00:00",
        protocol="ssh",
    )
    with patch.object(device_job_service, "submit_job", new=AsyncMock(return_value=fake_result)) as mocked:
        result = device_job_service.submit_job_sync(
            tenant_id="tenant-1",
            device_id="device-1",
            operation=DeviceOperation.GET_RUNNING_CONFIG,
            params={},
            requested_by="alice",
        )

    assert result is fake_result
    mocked.assert_awaited_once()
    _, kwargs = mocked.call_args
    assert kwargs["tenant_id"] == "tenant-1"
    assert kwargs["device_id"] == "device-1"
    assert kwargs["operation"] == DeviceOperation.GET_RUNNING_CONFIG
    assert kwargs["requested_by"] == "alice"


def test_submit_job_sync_propagates_failure():
    with patch.object(
        device_job_service,
        "submit_job",
        new=AsyncMock(side_effect=device_job_service.DeviceJobFailedError("device unreachable")),
    ):
        with pytest.raises(device_job_service.DeviceJobFailedError):
            device_job_service.submit_job_sync(
                tenant_id="t", device_id="d", operation=DeviceOperation.GET_RUNNING_CONFIG,
                params={}, requested_by="alice",
            )


def test_submit_job_sync_refuses_to_run_inside_a_running_event_loop():
    """Guards against a future caller wiring this into async request-handler
    code, where asyncio.run() would raise its own confusing error (or, in
    older Python, silently misbehave) instead of this clear one."""

    async def _call_from_loop():
        device_job_service.submit_job_sync(
            tenant_id="t", device_id="d", operation=DeviceOperation.GET_RUNNING_CONFIG,
            params={}, requested_by="alice",
        )

    with pytest.raises(RuntimeError, match="running event loop"):
        asyncio.run(_call_from_loop())

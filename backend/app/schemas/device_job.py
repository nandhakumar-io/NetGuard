"""Job envelope exchanged between the API (publisher) and the Device
Gateway (consumer/executor) over NATS.

Design goals (see Section 3 / Section 9 of the hardening spec):

  - The API must never hand the Gateway a raw command to run verbatim.
    It hands over a *declarative operation* (device_id, operation name,
    a small bag of typed parameters) that the Gateway itself maps to a
    concrete protocol call -- so a compromised API process gains "ask
    the Gateway to do a thing it already knows how to do to a device it
    already knows about", not "run arbitrary code/commands on the
    Gateway or on a device".
  - The envelope is signed (HMAC-SHA256 over a canonical payload) with a
    key ONLY the API and the Gateway hold (DEVICE_JOB_SIGNING_KEY) --
    not the Fernet key used for device credentials, and not the JWT
    SECRET_KEY used for user sessions, so compromising one doesn't
    compromise the others.
  - The Gateway independently re-validates everything in this envelope
    against its own DB read (tenant, device, approval, JIT state,
    expiry) -- it does NOT trust the API's say-so that a request is
    authorized. See device_gateway/validator.py.
  - `job_id` + `expires_at` provide replay protection: the Gateway
    tracks completed/in-flight job_ids for a bounded window and refuses
    to execute the same job_id twice, and refuses any job whose
    expires_at has passed by the time it's picked up.

This is intentionally a *narrow* set of operations, not a general RPC
mechanism. Adding a new operation means adding a new case in
device_gateway/executor.py, not widening what the API is allowed to ask
for.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class DeviceOperation(str, Enum):
    """Whitelist of operations the Gateway knows how to perform. The API
    can only ever request one of these -- there is no "run this string"
    member, deliberately."""

    GET_RUNNING_CONFIG = "get_running_config"
    GET_STARTUP_CONFIG = "get_startup_config"
    DEPLOY_CONFIG = "deploy_config"
    ROLLBACK_CONFIG = "rollback_config"
    REBOOT = "reboot"

    # Read-only NAPALM-getter operations added to close the
    # health_monitor.py gap: post-deploy monitoring (pipeline_service's
    # run_monitoring_window) used to decrypt the device's SSH credential
    # in-worker and open a NAPALM connection directly for these checks.
    # Routing them through the Gateway means the worker process handling
    # a deployment never holds a device credential for this, same as the
    # config-read/deploy/rollback operations above.
    GET_FACTS = "get_facts"
    GET_BGP_NEIGHBORS = "get_bgp_neighbors"
    GET_OSPF_NEIGHBORS = "get_ospf_neighbors"
    GET_VPN_STATUS = "get_vpn_status"

    # SNMP health poll (app.services.metrics_service.poll_device). Added
    # alongside the DEVICE_CREDENTIAL_ENCRYPTION_KEY re-scoping: SNMP
    # community/v3 auth/priv credentials moved to the Gateway-only key,
    # so `api`'s background poll loop and the `poller` Celery worker can
    # no longer decrypt them directly -- this lets both dispatch a job
    # instead, same as every other device-facing operation.
    SNMP_POLL = "snmp_poll"


# Operations that touch device state rather than just reading it -- used
# by the Gateway to decide whether an approval_id is mandatory even if
# the caller forgot to require one (belt-and-suspenders on top of the
# API's own risk-classification/approval gate).
MUTATING_OPERATIONS = {
    DeviceOperation.DEPLOY_CONFIG,
    DeviceOperation.ROLLBACK_CONFIG,
    DeviceOperation.REBOOT,
}


class DeviceJobRequest(BaseModel):
    job_id: str  # uuid4, unique per request -- replay-protection key
    tenant_id: str
    device_id: str
    operation: DeviceOperation
    params: dict = Field(default_factory=dict)  # e.g. {"config_text": "..."} for DEPLOY_CONFIG

    requested_by: str  # user id (not email -- stable even if email changes)
    change_request_id: str | None = None
    approval_id: str | None = None
    jit_elevation_id: str | None = None

    issued_at: str  # ISO8601 UTC
    expires_at: str  # ISO8601 UTC -- short-lived, minutes not hours

    signature: str = ""  # filled in by sign(), empty while building the payload to sign


def _canonical_payload(job: DeviceJobRequest) -> bytes:
    """Deterministic byte representation of every field EXCEPT the
    signature itself, so sign()/verify() compute over identical bytes."""
    data = job.model_dump(exclude={"signature"})
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign(job: DeviceJobRequest, key: str) -> DeviceJobRequest:
    mac = hmac.new(key.encode("utf-8"), _canonical_payload(job), hashlib.sha256).hexdigest()
    job.signature = mac
    return job


def verify_signature(job: DeviceJobRequest, key: str) -> bool:
    """Constant-time compare. A job with a missing/invalid signature must
    never be executed, regardless of how well-formed the rest of it looks
    -- this is what stops a compromised API process (or anything else
    that can publish to NATS if a subject-ACL is ever misconfigured) from
    forging jobs the Gateway would otherwise treat as authentic."""
    if not job.signature:
        return False
    expected = hmac.new(key.encode("utf-8"), _canonical_payload(job), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, job.signature)


def is_expired(job: DeviceJobRequest, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    try:
        expires = datetime.fromisoformat(job.expires_at)
    except ValueError:
        return True  # malformed timestamp -- fail closed
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return now >= expires


class DeviceJobResult(BaseModel):
    job_id: str
    success: bool
    output: str = ""
    error: str | None = None
    executed_at: str  # ISO8601 UTC
    correlation_id: str | None = None  # ties back to a ProtocolOperation row, if one was recorded
    protocol: str | None = None  # "ssh" | "netconf" | "restconf", when the executor used protocol_manager
    execution_time_ms: float | None = None

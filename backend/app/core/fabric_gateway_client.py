"""Thin HTTP client for the fabric-gateway sidecar (see fabric/gateway/).

Deliberately the ONLY module in the FastAPI/Celery processes that knows
the sidecar's wire format. app.services.fabric_service is the only
caller -- everything else in NetGuard talks to fabric_service's
anchor_evidence()/get_evidence()/verify_evidence()/get_evidence_history()
and never imports this module directly (Section 4 of the spec: "the rest
of NetGuard should never need to know how Fabric Gateway/SDK calls are
implemented").

Why a sidecar instead of an in-process Fabric SDK call: the only
maintained Fabric Gateway client SDKs are Go/Node/Java (no official
Python SDK), and vendoring a JVM/Node runtime into the FastAPI/Celery
images purely to hold Fabric connection state would tightly couple the
API process to the Fabric runtime -- exactly what the spec says to
avoid. The sidecar is a small Node service (fabric/gateway/) that holds
the actual `@hyperledger/fabric-gateway` gateway connection and exposes
a narrow REST surface; this client just calls it.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class FabricGatewayError(RuntimeError):
    """Raised for any non-2xx response or transport failure talking to
    the fabric-gateway sidecar. fabric_service treats every instance of
    this as "Fabric currently unavailable" -- never as evidence of a
    ledger-side rejection, which the sidecar instead reports as a normal
    2xx response with an error payload (see submit_evidence)."""


class FabricUnconfiguredError(RuntimeError):
    """Raised when FABRIC_ENABLED is true but FABRIC_GATEWAY_URL/
    FABRIC_GATEWAY_API_KEY are not set -- a misconfiguration, distinct
    from FabricGatewayError (which means "configured but unreachable")."""


def _client() -> httpx.Client:
    if not settings.FABRIC_GATEWAY_URL or not settings.FABRIC_GATEWAY_API_KEY:
        raise FabricUnconfiguredError(
            "FABRIC_ENABLED=true but FABRIC_GATEWAY_URL/FABRIC_GATEWAY_API_KEY are not both set"
        )
    return httpx.Client(
        base_url=settings.FABRIC_GATEWAY_URL,
        timeout=settings.FABRIC_GATEWAY_TIMEOUT_SECONDS,
        headers={"Authorization": f"Bearer {settings.FABRIC_GATEWAY_API_KEY}"},
    )


def submit_evidence(record: dict[str, Any]) -> dict[str, Any]:
    """POST /evidence -> CreateEvidence on netguard-evidence chaincode.

    `record` is the small, non-sensitive ledger record described in
    Section 2/7 of the spec (evidence_id, evidence_hash,
    configuration_hash, change_request_id, device_id, evidence_type,
    result, versions, timestamp, actor_subject) -- NEVER the full
    evidence_body. Idempotent on the chaincode side keyed by
    evidence_id (Section 19): calling this twice for the same
    evidence_id returns the existing transaction rather than creating a
    duplicate ledger entry, so fabric_service does not need its own
    dedup logic beyond checking anchor_status before calling this.

    Returns {"transaction_id": ..., "block_number": ...}.
    """
    with _client() as client:
        try:
            resp = client.post("/evidence", json=record)
        except httpx.HTTPError as exc:
            raise FabricGatewayError(f"fabric-gateway unreachable: {exc}") from exc
    if resp.status_code >= 500:
        raise FabricGatewayError(f"fabric-gateway {resp.status_code}: {resp.text[:500]}")
    if resp.status_code >= 400:
        # 4xx from the sidecar means the chaincode itself rejected the
        # submission (e.g. malformed record) -- not a transient outage,
        # so callers should treat this as a hard FAILED, not PENDING/retry.
        raise FabricGatewayError(f"chaincode rejected evidence: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def get_evidence(evidence_id: str) -> dict[str, Any] | None:
    """GET /evidence/{id} -> GetEvidence. Returns None if not found
    (404), raises FabricGatewayError for any other failure."""
    with _client() as client:
        try:
            resp = client.get(f"/evidence/{evidence_id}")
        except httpx.HTTPError as exc:
            raise FabricGatewayError(f"fabric-gateway unreachable: {exc}") from exc
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise FabricGatewayError(f"fabric-gateway {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def get_evidence_history(evidence_id: str) -> list[dict[str, Any]]:
    """GET /evidence/{id}/history -> GetEvidenceHistory."""
    with _client() as client:
        try:
            resp = client.get(f"/evidence/{evidence_id}/history")
        except httpx.HTTPError as exc:
            raise FabricGatewayError(f"fabric-gateway unreachable: {exc}") from exc
    if resp.status_code >= 400:
        raise FabricGatewayError(f"fabric-gateway {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def get_evidence_by_change_request(change_request_id: str) -> list[dict[str, Any]]:
    """GET /change-requests/{id}/evidence -> GetEvidenceByChangeRequest."""
    with _client() as client:
        try:
            resp = client.get(f"/change-requests/{change_request_id}/evidence")
        except httpx.HTTPError as exc:
            raise FabricGatewayError(f"fabric-gateway unreachable: {exc}") from exc
    if resp.status_code >= 400:
        raise FabricGatewayError(f"fabric-gateway {resp.status_code}: {resp.text[:500]}")
    return resp.json()

"""HTTP client for the fabric-gateway sidecar.

The main FastAPI process never imports the Hyperledger Fabric SDK directly --
a thin Python sidecar (``fabric/gateway/``) wraps the SDK and exposes a small
JSON REST API.  This module is the only place in the main app that knows the
wire format of that API; all other code goes through ``fabric_service.py``,
which calls these helpers.

Error hierarchy
---------------
``FabricUnconfiguredError``
    Raised when ``FABRIC_ENABLED=True`` but ``FABRIC_GATEWAY_URL`` or
    ``FABRIC_GATEWAY_API_KEY`` is missing.  This is a deployment
    misconfiguration (permanent), not a transient outage -- callers must
    NOT retry it.

``FabricGatewayError``
    All other unexpected responses (4xx that aren't 409, 5xx, network
    errors, JSON parse failures).  Treated as transient by
    ``anchor_evidence_task`` which retries with exponential backoff.

``FabricDuplicateError(FabricGatewayError)``
    The sidecar returned 409 CONFLICT because the ``evidence_id`` is
    already on the ledger.  ``fabric_service`` treats this as a
    successful idempotent no-op (Section 19).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public exception hierarchy
# ---------------------------------------------------------------------------


class FabricGatewayError(Exception):
    """Transport or sidecar-side error -- transient, worth retrying."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class FabricDuplicateError(FabricGatewayError):
    """Sidecar returned 409 -- evidence_id already on the ledger.

    ``fabric_service`` treats this as a successful idempotent no-op:
    the ledger already has the record, so there is nothing left to do.
    """


class FabricUnconfiguredError(FabricGatewayError):
    """``FABRIC_ENABLED=True`` but required config is missing.

    This is a deployment misconfiguration (permanent), *not* a transient
    outage -- callers must NOT schedule retries for this error.
    """

    def __init__(self):
        super().__init__(
            "Fabric is enabled but FABRIC_GATEWAY_URL or FABRIC_GATEWAY_API_KEY is "
            "not set -- check your environment configuration.",
            status_code=None,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    """Build the request headers, validating config first."""
    if not settings.FABRIC_ENABLED:
        raise FabricUnconfiguredError()
    if not settings.FABRIC_GATEWAY_URL:
        raise FabricUnconfiguredError()
    if not settings.FABRIC_GATEWAY_API_KEY:
        raise FabricUnconfiguredError()
    return {
        "X-API-Key": settings.FABRIC_GATEWAY_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _base_url() -> str:
    url = (settings.FABRIC_GATEWAY_URL or "").rstrip("/")
    return url


def _handle_response(resp: httpx.Response, *, context: str) -> dict[str, Any]:
    """Raise the appropriate exception if the response is unsuccessful,
    otherwise return the parsed JSON body."""
    if resp.status_code == 409:
        logger.info("fabric-gateway: 409 Conflict on %s -- treating as duplicate (idempotent)", context)
        raise FabricDuplicateError(f"Evidence already on ledger: {context}", status_code=409)

    if not resp.is_success:
        body_snippet = resp.text[:200] if resp.text else "<empty>"
        logger.error(
            "fabric-gateway: HTTP %d on %s -- body: %s", resp.status_code, context, body_snippet
        )
        raise FabricGatewayError(
            f"fabric-gateway returned HTTP {resp.status_code} for {context}: {body_snippet}",
            status_code=resp.status_code,
        )

    try:
        return resp.json()
    except Exception as exc:
        raise FabricGatewayError(
            f"fabric-gateway returned non-JSON body for {context}: {resp.text[:200]}"
        ) from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def submit_evidence(ledger_record: dict[str, Any]) -> dict[str, Any]:
    """Anchor a new evidence record on the Fabric ledger.

    Parameters
    ----------
    ledger_record:
        The payload the sidecar will pass to the chaincode
        ``CreateEvidence`` function.  Must include ``evidence_id``,
        ``evidence_hash``, ``evidence_type``, ``change_request_id``,
        ``actor_subject``, and ``created_at`` at a minimum.

    Returns
    -------
    dict with at least::

        {
            "transaction_id": "<fabric-tx-id>",
            "block_number":   12345,
            "evidence_id":    "<same id as input>"
        }

    Raises
    ------
    FabricUnconfiguredError
        ``FABRIC_ENABLED`` is True but ``FABRIC_GATEWAY_URL`` / API key unset.
    FabricDuplicateError
        Sidecar returned 409 (evidence_id already on ledger).
    FabricGatewayError
        Any other unexpected error.
    """
    headers = _headers()
    url = f"{_base_url()}/evidence"
    try:
        with httpx.Client(timeout=settings.FABRIC_GATEWAY_TIMEOUT_SECONDS) as client:
            resp = client.post(url, json=ledger_record, headers=headers)
    except httpx.RequestError as exc:
        raise FabricGatewayError(f"Network error contacting fabric-gateway: {exc}") from exc

    result = _handle_response(resp, context=f"POST /evidence [{ledger_record.get('evidence_id')}]")
    logger.info(
        "fabric-gateway: evidence %s anchored — tx=%s block=%s",
        result.get("evidence_id"),
        result.get("transaction_id"),
        result.get("block_number"),
    )
    return result


def get_evidence(evidence_id: str) -> dict[str, Any] | None:
    """Retrieve a single evidence record from the Fabric ledger.

    Returns
    -------
    The chaincode's ledger record, or ``None`` if the sidecar returns 404
    (evidence not yet anchored, e.g., sidecar restarted between submit and
    this read).

    Raises
    ------
    FabricUnconfiguredError / FabricGatewayError
        Same semantics as :func:`submit_evidence`.
    """
    headers = _headers()
    url = f"{_base_url()}/evidence/{evidence_id}"
    try:
        with httpx.Client(timeout=settings.FABRIC_GATEWAY_TIMEOUT_SECONDS) as client:
            resp = client.get(url, headers=headers)
    except httpx.RequestError as exc:
        raise FabricGatewayError(f"Network error contacting fabric-gateway: {exc}") from exc

    if resp.status_code == 404:
        logger.debug("fabric-gateway: evidence %s not found on ledger (404)", evidence_id)
        return None

    return _handle_response(resp, context=f"GET /evidence/{evidence_id}")


def get_evidence_history(evidence_id: str) -> list[dict[str, Any]]:
    """Return the full ledger history for an evidence record (all writes).

    Hyperledger Fabric's key history enables forensic audit: every state
    transition for ``evidence_id`` is returned in chronological order, so
    callers can prove the record was never modified after anchoring.

    Returns
    -------
    List of history entries (may be empty if the sidecar returns 404).

    Raises
    ------
    FabricUnconfiguredError / FabricGatewayError
        Same semantics as :func:`submit_evidence`.
    """
    headers = _headers()
    url = f"{_base_url()}/evidence/{evidence_id}/history"
    try:
        with httpx.Client(timeout=settings.FABRIC_GATEWAY_TIMEOUT_SECONDS) as client:
            resp = client.get(url, headers=headers)
    except httpx.RequestError as exc:
        raise FabricGatewayError(f"Network error contacting fabric-gateway: {exc}") from exc

    if resp.status_code == 404:
        logger.debug("fabric-gateway: history for %s not found (404)", evidence_id)
        return []

    result = _handle_response(resp, context=f"GET /evidence/{evidence_id}/history")
    # Sidecar may return {"history": [...]} or directly a list.
    if isinstance(result, list):
        return result
    return result.get("history", [])
